#!/usr/bin/env python3
"""MCP stdio server: web_search + web_fetch + key_status backed by Exa.

Upstream: POST https://mcp.exa.ai/mcp (JSON-RPC over Streamable HTTP), same
mechanism opencode uses (packages/core/src/tool/websearch.ts in sst/opencode).

Key pool / rotation:
- Keys come from EXA_API_KEYS env var (comma-separated) or, if unset, from
  ~/.config/web-search-proxy/env (chmod 600). No keys -> keyless free endpoint.
- Round-robin across keys. A key is usable iff now >= its retry_at.
- Exa hides API errors inside HTTP-200 MCP replies as
  "web_search_exa error (<code>): <message>", so classification reads the
  embedded code (HTTP-level statuses handled too):
    401 INVALID_API_KEY        -> invalid, recheck after 24h
    402 NO_MORE_CREDITS etc.   -> exhausted ($10/month pot empty),
                                  probe-retry every PROBE_INTERVAL; success
                                  on probe = refill detected -> active again
    429                        -> per-second QPS limit, brief cooldown only
    other/network              -> short penalty, try next key
- When no key is usable, falls back to the keyless endpoint instead of failing.
- Exa exposes NO balance/credits API (admin endpoints don't exist), so refill
  detection is necessarily probe-based.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

EXA_MCP_URL = "https://mcp.exa.ai/mcp"
ENV_FILE = os.path.expanduser("~/.config/web-search-proxy/env")
STATE_FILE = os.path.expanduser("~/.local/share/muse-filter/exa_keys.json")

SEARCH_TIMEOUT = 30   # seconds; livecrawl can be slow
FETCH_TIMEOUT = 45
PROBE_INTERVAL = 3600      # exhausted key: allow a probe request every hour
INVALID_RETRY = 86400      # invalid key: recheck once a day
QPS_BACKOFF = 15           # 429: brief cooldown on that key only
TRANSIENT_BACKOFF = 60     # network/5xx penalty before key is retried
UA = "web-search-proxy/2.1"  # Exa's edge 403s bare Python-urllib UA

# Adapter runs ThreadingHTTPServer; rotation state is read-modify-write JSON.
_ROTATE_LOCK = threading.Lock()


# ---------- config / state ----------

def load_keys():
    raw = os.environ.get("EXA_API_KEYS", "").strip()
    if not raw and os.path.isfile(ENV_FILE):
        try:
            with open(ENV_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    if k == "EXA_API_KEYS" or (k == "EXA_API_KEY" and not raw):
                        raw = v.strip()
        except OSError:
            pass
    keys, seen = [], set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part and part not in seen:
            seen.add(part)
            keys.append(part)
    return keys


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


_STATE_WARNED = False


def save_state(state):
    global _STATE_WARNED
    tmp = STATE_FILE + ".tmp"
    try:
        # Auto-create the parent dir so a bare script run (no install.sh)
        # still persists rotation state instead of silently dropping it.
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        if not _STATE_WARNED:
            _STATE_WARNED = True
            sys.stderr.write(
                f"web-search-proxy: cannot persist state to {STATE_FILE}: {e}\n")


def mask(key):
    return key[:8] + "\u2026" + key[-4:] if len(key) > 14 else key[:4] + "\u2026"


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def human_delta(seconds):
    if seconds <= 0:
        return "now"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"in {h}h{m:02d}m"
    if m:
        return f"in {m}m{s:02d}s"
    return f"in {s}s"


# ---------- exa transport ----------

def _http_post(url, payload, timeout):
    """Returns (http_status_or_None, body, network_error_or_None)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body, None
    except Exception as e:
        return None, "", str(e)


def _extract_text(body):
    """Parse direct-JSON or SSE reply -> (text, is_error, embedded_code_or_None, err_str)."""
    candidates = []
    stripped = body.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    else:
        for line in body.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                candidates.append(line[5:].strip())
    for cand in candidates:
        try:
            msg = json.loads(cand)
        except ValueError:
            continue
        result = msg.get("result") or {}
        contents = result.get("content")
        if isinstance(contents, list):
            texts = [c.get("text", "") for c in contents if isinstance(c, dict)]
            text = "\n".join(t for t in texts if t)
            if text:
                # Exa wraps upstream errors at the start of the text:
                # "web_search_exa error (401): Invalid API key". Anchor to
                # the head and whitelist only exa tool-name prefixes so
                # result content like "HTTP error (404): Not Found" at the
                # start of a page is not misread as an API failure.
                m = re.search(r"^(?:(?:web_search_exa|web_fetch_exa) )?error \((\d{3})\)", text)
                if result.get("isError") or m:
                    return text, True, int(m.group(1)) if m else None, text[:300]
                return text, False, None, None
        if msg.get("error"):
            return "", True, None, str(msg["error"])[:300]
    return "", False, None, "no content in response"


def _attempt(url, tool, args):
    """One HTTP attempt against one URL. Returns (ok, text, code, detail)."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": args}}
    status, body, net_err = _http_post(url, payload,
                                       SEARCH_TIMEOUT if tool == "web_search_exa" else FETCH_TIMEOUT)
    if net_err:
        return False, "", None, f"network: {net_err}"
    text, is_err, embed_code, detail = _extract_text(body)
    if not is_err and text:
        return True, text, None, None
    code = embed_code if embed_code is not None else (status if isinstance(status, int) and status >= 400 else None)
    if code is None:
        code = 500
        detail = detail or "unclassified empty reply"
    return False, text, code, (detail or text)[:300]


# ---------- rotation engine ----------

def call_with_rotation(tool, args):
    """Try configured keys round-robin; fall back to keyless. Returns final text.

    The lock guards only the shared-state read/modify/write; network calls run
    outside it so concurrent requests (the adapter is a threaded server) do not
    serialize behind a slow upstream search.
    """
    keys = load_keys()
    now = time.time()

    with _ROTATE_LOCK:
        state = load_state()
        live = set(keys)
        for k in [k for k in state if k not in live and k != "_cursor"]:
            del state[k]
        cursor = int(state.get("_cursor", 0)) % max(len(keys), 1)
        # snapshot per-key cooldowns; two racing callers may pick the same key,
        # which is harmless (round-robin stays approximately fair, state stays
        # consistent because each mutation re-reads under the lock)
        plan = []
        for key in ([keys[(cursor + i) % len(keys)] for i in range(len(keys))]
                    if keys else []):
            st = state.setdefault(key, {"status": "active", "retry_at": 0, "ok": 0,
                                        "fail": 0, "recovered": 0, "last_error": "",
                                        "last_used": 0})
            plan.append((key, st.get("status") == "exhausted",
                         now < st.get("retry_at", 0), st.get("status", "active"),
                         st.get("retry_at", 0)))
        if plan:
            save_state(state)

    notes = []
    for key, was_exhausted, cooling, status, retry_at in plan:
        if cooling:
            notes.append(f"{mask(key)}: cooling ({status}, "
                         f"{human_delta(retry_at - now)})")
            continue

        url = f"{EXA_MCP_URL}?exaApiKey={urllib.parse.quote(key)}"
        ok, text, code, detail = _attempt(url, tool, args)

        with _ROTATE_LOCK:
            state = load_state()
            st = state.setdefault(key, {"status": "active", "retry_at": 0, "ok": 0,
                                        "fail": 0, "recovered": 0, "last_error": "",
                                        "last_used": 0})
            st["last_used"] = now
            if ok:
                st["status"] = "active"
                st["retry_at"] = 0
                st["ok"] += 1
                if was_exhausted:
                    st["recovered"] += 1  # credits refilled; probe succeeded
                st["last_error"] = ""
                state["_cursor"] = (keys.index(key) + 1) % len(keys)
                save_state(state)
            else:
                st["fail"] += 1
                st["last_error"] = f"{code}: {detail}" if detail else str(code)
                if code == 401:
                    st["status"] = "invalid"
                    st["retry_at"] = now + INVALID_RETRY
                elif code == 402:
                    st["status"] = "exhausted"
                    st["retry_at"] = now + PROBE_INTERVAL
                elif code == 429:
                    st["status"] = "active"      # healthy, just throttled this second
                    st["retry_at"] = now + QPS_BACKOFF
                else:
                    st["status"] = "active"
                    st["retry_at"] = now + TRANSIENT_BACKOFF
                save_state(state)
        if ok:
            prefix = f"[key {mask(key)} recovered - credits refilled]\n" if was_exhausted else ""
            return prefix + text
        notes.append(f"{mask(key)}: fail {code} ({(detail or '')[:80]})")

    # no usable key produced a result -> keyless fallback
    used_fallback = bool(keys)
    ok, text, code, detail = _attempt(EXA_MCP_URL, tool, args)
    if ok:
        prefix = "[all API keys unavailable - served keyless]\n" if used_fallback else ""
        return prefix + text

    lines = ["error: all Exa paths failed."]
    lines += [f"- {n}" for n in notes]
    if used_fallback:
        lines.append(f"- keyless fallback: fail {code} ({(detail or '')[:120]})")
    lines.append(f"checked at {iso(now)}; cooled keys auto-retry per schedule (key_status tool)")
    return "\n".join(lines)


def key_status():
    keys = load_keys()
    state = load_state()
    now = time.time()
    src = "env var EXA_API_KEYS" if os.environ.get("EXA_API_KEYS") else (
        ENV_FILE if os.path.isfile(ENV_FILE) else "(none)")
    out = [f"Exa key pool: {len(keys)} key(s), source: {src}",
           "keyless fallback: enabled (free tier)", ""]
    if not keys:
        out.append("no keys configured - running keyless only")
    for i, key in enumerate(keys):
        st = state.get(key, {})
        status = st.get("status", "active")
        retry_at = st.get("retry_at", 0)
        line = (f"{i + 1}. {mask(key)}  status={status}  "
                f"ok={st.get('ok', 0)} fail={st.get('fail', 0)} "
                f"refill_recoveries={st.get('recovered', 0)}")
        if now < retry_at:
            line += f"\n   next usage available at {iso(retry_at)} ({human_delta(retry_at - now)})"
        if st.get("last_used"):
            line += f"\n   last used {iso(st['last_used'])}"
        if st.get("last_error"):
            line += f"\n   last error: {st['last_error'][:200]}"
        marker = " <- next in rotation" if keys and i == int(state.get("_cursor", 0)) % len(keys) else ""
        out.append(line + marker)
    return "\n".join(out)


# ---------- MCP plumbing ----------

TOOLS = [
    {"name": "web_search", "description": "Search the web via Exa (rotating API-key pool, keyless fallback) for current information.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Search query"},
         "max_results": {"type": "integer", "description": "Max results", "default": 5}},
         "required": ["query"]}},
    {"name": "web_fetch", "description": "Fetch a URL and return its content as clean markdown via Exa.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "URL to fetch"}},
         "required": ["url"]}},
    {"name": "key_status", "description": "Show Exa API key pool rotation status (keys masked): states, counters, next retry times.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def handle(req):
    method = req.get("method")
    params = req.get("params", {}) or {}
    id = req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "web-search-proxy", "version": "2.1"}}}
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        try:
            if name == "web_search":
                query = args.get("query", "")
                if not query.strip():
                    text = "error: empty query"
                else:
                    text = call_with_rotation("web_search_exa", {
                        "query": query,
                        "numResults": max(int(args.get("max_results", 5)), 1),
                        "type": "auto",
                        "livecrawl": "fallback"})
            elif name == "web_fetch":
                url = args.get("url", "")
                if not url.strip():
                    text = "error: empty url"
                else:
                    text = call_with_rotation("web_fetch_exa", {
                        "urls": [url], "maxCharacters": 8000})
            elif name == "key_status":
                text = key_status()
            else:
                return {"jsonrpc": "2.0", "id": id,
                        "error": {"code": -32601, "message": f"unknown tool: {name}"}}
        except Exception as e:
            text = f"error: {e}"
        return {"jsonrpc": "2.0", "id": id,
                "result": {"content": [{"type": "text", "text": text[:20000]}]}}
    if id is None:
        return None
    return {"jsonrpc": "2.0", "id": id, "error": {"code": -32601,
            "message": f"method not found: {method}"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError as e:
            sys.stderr.write(f"bad json: {e}\n")
            continue
        try:
            resp = handle(req)
        except Exception as e:
            resp = {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
