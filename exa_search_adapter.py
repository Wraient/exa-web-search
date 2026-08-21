#!/usr/bin/env python3
"""Local adapter that makes Grok Build's NATIVE web_search tool run on Exa.

Grok Build resolves [models].web_search to a model entry and calls it via the
OpenAI Responses API: POST {base_url}/responses with {"input": "<query>",
"tools": [{"type": "web_search", ...}]}. This server implements that endpoint
and answers every request with Exa search results (rotating API-key pool +
keyless fallback reused from mcp_web_search.py).

Wired in ~/.grok/config.toml:
    [models]
    web_search = "exa-search-local"
    [model.exa-search-local]
    base_url = "http://127.0.0.1:8390/v1"
    api_backend = "responses"

Runs as systemd user service exa-search-adapter.service.
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_web_search import call_with_rotation  # noqa: E402  (rotation engine)

PORT = int(os.environ.get("EXA_ADAPTER_PORT", "8390"))
LOG = os.path.expanduser("~/.local/share/muse-filter/exa_adapter.log")


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n")
    except OSError:
        pass


def extract_query(payload):
    """Pull the search query out of a Responses-API request body."""
    q = payload.get("input")
    if isinstance(q, str):
        return q.strip()
    if isinstance(q, list):
        parts = []
        for item in q:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                c = item.get("content")
                if isinstance(c, str):
                    parts.append(c)
                elif isinstance(c, list):
                    parts += [x.get("text", "") for x in c if isinstance(x, dict)]
        return "\n".join(p for p in parts if p).strip()
    return ""


def do_search(query):
    """Run the query through the Exa rotation engine; return result text."""
    t0 = time.time()
    try:
        text = call_with_rotation("web_search_exa", {
            "query": query,
            "numResults": 6,
            "type": "auto",
            "livecrawl": "fallback",
        })
        dt = time.time() - t0
        log(f"search ok ({dt:.1f}s): {query[:80]!r}")
        return text[:15000]
    except Exception as e:
        log(f"search FAILED: {query[:80]!r}: {e}")
        return f"Web search error: {e}"


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": "exa-search-local",
                                                         "object": "model"}]})
        else:
            self._json(200, {"status": "ok", "service": "exa-search-adapter"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n)) if n else {}
        except ValueError:
            payload = {}

        query = extract_query(payload)
        if not query:
            self._json(200, self._resp("(empty search query)"))
            return
        self._json(200, self._resp(do_search(query)))

    def _resp(self, text):
        # Mirrors the exact Responses-API payload shape Grok Build accepts
        # (captured from a real xAI backend-search reply via MITM).
        ts = int(time.time())
        uid = f"{ts:x}-exa"
        return {
            "created_at": ts,
            "completed_at": ts,
            "id": uid,
            "max_output_tokens": 8192,
            "model": "exa-search-local",
            "object": "response",
            "output": [{
                "content": [{"type": "output_text", "text": text,
                             "logprobs": [], "annotations": []}],
                "id": f"msg_{uid}",
                "role": "assistant",
                "type": "message",
                "status": "completed",
            }],
            "parallel_tool_calls": True,
            "previous_response_id": None,
            "reasoning": {"effort": None, "summary": None},
            "temperature": 0.1,
            "text": {"format": {"type": "text"}},
            "tool_choice": "auto",
            "tools": [],
            "top_p": 0.95,
            "usage": {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": max(len(text) // 4, 1),
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 10 + max(len(text) // 4, 1),
                "num_sources_used": 0,
                "num_server_side_tools_used": 0,
            },
            "user": None,
            "incomplete_details": None,
            "status": "completed",
            "store": False,
            "metadata": {},
            "background": False,
            "service_tier": "default",
            "truncation": "disabled",
            "top_logprobs": 0,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
        }

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    log(f"adapter listening on 127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
