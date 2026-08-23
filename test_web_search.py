"""Regression tests for mcp_web_search / exa_search_adapter.

Run: python3 test_web_search.py
"""
import importlib
import json
import os
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
mws = importlib.import_module("mcp_web_search")
adapter = importlib.import_module("exa_search_adapter")

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


# ---- _extract_text: error classification ----
def sse(msg):
    return "event: message\ndata: " + json.dumps(msg) + "\n\n"


ok_reply = {"result": {"content": [{"type": "text", "text": "# Results\nerror (404) not found page"}]}}

t, e, c, d = mws._extract_text(json.dumps(ok_reply))
check("benign content containing 'error (404)' is NOT an error (regex anchored)", not e and c is None)

err_reply = {"result": {"content": [{"type": "text",
        "text": "web_search_exa error (401): Invalid API key"}], "isError": True}}
t, e, c, d = mws._extract_text(json.dumps(err_reply))
check("real Exa wrapper 'web_search_exa error (401)' IS classified 401", e and c == 401)

err2 = {"result": {"content": [{"type": "text",
        "text": "error (402): NO_MORE_CREDITS"}], "isError": True}}
t, e, c, d = mws._extract_text(json.dumps(err2))
check("bare 'error (402)' at start classified 402", e and c == 402)

t, e, c, d = mws._extract_text(sse(ok_reply))
check("SSE-wrapped ok reply parsed, not error", (not e) and t.startswith("# Results"))

t, e, c, d = mws._extract_text(json.dumps({"error": {"code": -1, "message": "boom"}}))
check("jsonrpc top-level error detected", e)

t, e, c, d = mws._extract_text("garbage")
check("garbage body -> not error, no-content detail", (not e) and d == "no content in response")

# ---- _attempt: empty 200 must not use status 200 as error code ----
with mock.patch.object(mws, "_http_post", return_value=(200, json.dumps(
        {"result": {"content": []}}), None)):
    ok, text, code, detail = mws._attempt(mws.EXA_MCP_URL, "web_search_exa", {})
    check("empty 200 reply -> code 500/None, never 200", code != 200 and not ok)

# ---- rotation engine with mocked attempts ----
tmp = tempfile.mkdtemp()
state_file = os.path.join(tmp, "exa_keys.json")
mws.STATE_FILE = state_file
mws.ENV_FILE = os.path.join(tmp, "env")

keys = ["k00000000000000001", "k00000000000000002"]
with open(mws.ENV_FILE, "w") as f:
    f.write("EXA_API_KEYS=" + ",".join(keys) + "\n")

GOOD = json.dumps({"result": {"content": [{"type": "text", "text": "SEARCH-OK"}]}})
BAD401 = json.dumps({"result": {"content": [{"type": "text",
                "text": "web_search_exa error (401): Invalid API key"}], "isError": True}})

# Happy path: round robin across keys
with mock.patch.object(mws, "_http_post", return_value=(200, GOOD, None)) as hp:
    a = mws.call_with_rotation("web_search_exa", {"query": "q"})
    b = mws.call_with_rotation("web_search_exa", {"query": "q"})
    st = json.load(open(state_file))
    check("round robin: both keys used once each",
          st[keys[0]]["ok"] == 1 and st[keys[1]]["ok"] == 1)
    check("happy path returns text", a == "SEARCH-OK" == b)
    urls = [c.args[0] for c in hp.call_args_list]
    check("api key in query string differs per call", urls[0] != urls[1])

# 401 benches key, falls through to next key, then keyless fallback
with mock.patch.object(mws, "_http_post", return_value=(200, BAD401, None)):
    out = mws.call_with_rotation("web_search_exa", {"query": "q"})
    st = json.load(open(state_file))
    check("401 marks key invalid", st[keys[0]]["status"] == "invalid")

# All keys cooling -> keyless fallback used, result prefixed
for k in keys:
    st = json.load(open(state_file))
    st[k]["retry_at"] = time.time() + 9999
    json.dump(st, open(state_file, "w"))
with mock.patch.object(mws, "_http_post", return_value=(200, GOOD, None)) as hp:
    out = mws.call_with_rotation("web_search_exa", {"query": "q"})
    check("cooling keys -> keyless fallback with prefix",
          out.startswith("[all API keys unavailable - served keyless]"))
    check("keyless call has no api key param", "exaApiKey" not in hp.call_args.args[0])

# state pruning of removed keys
st = json.load(open(state_file))
st["REMOVEDKEY"] = {"status": "active"}
json.dump(st, open(state_file, "w"))
with mock.patch.object(mws, "_http_post", return_value=(200, GOOD, None)):
    mws.call_with_rotation("web_search_exa", {"query": "q"})
st = json.load(open(state_file))
check("state entries for removed keys pruned", "REMOVEDKEY" not in st)

# ---- concurrency: parallel rotations must not corrupt state ----
st = json.load(open(state_file))
for k in keys:
    st[k] = {"status": "active", "retry_at": 0, "ok": 0, "fail": 0,
             "recovered": 0, "last_error": "", "last_used": 0}
json.dump(st, open(state_file, "w"))
with mock.patch.object(mws, "_http_post", return_value=(200, GOOD, None)):
    threads = [threading.Thread(target=mws.call_with_rotation,
                                args=("web_search_exa", {"query": "q"}))
               for _ in range(20)]
    [t_.start() for t_ in threads]
    [t_.join() for t_ in threads]
st = json.load(open(state_file))
total_ok = st[keys[0]]["ok"] + st[keys[1]]["ok"]
check(f"20 concurrent calls -> all 20 counted (got {total_ok})", total_ok == 20)
check("state file is valid JSON after concurrent writes", isinstance(st, dict))

# ---- MCP handle() ----
r = mws.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
check("tools/list exposes 3 tools", len(r["result"]["tools"]) == 3)
r = mws.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "web_search", "arguments": {"query": "   "}}})
check("empty query rejected", "empty query" in r["result"]["content"][0]["text"])
r = mws.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "nope", "arguments": {}}})
check("unknown tool -> -32601", r["error"]["code"] == -32601)
r = mws.handle({"jsonrpc": "2.0", "id": 4, "method": "ping"})
check("ping", r["result"] == {})

# ---- adapter helpers ----
check("extract_query str", adapter.extract_query({"input": " hi "}) == "hi")
check("extract_query list-of-blocks", adapter.extract_query(
    {"input": [{"role": "user", "content": [{"type": "text", "text": "a"},
                                            {"type": "text", "text": "b"}]}]}) == "a\nb")
check("extract_query empty", adapter.extract_query({}) == "")
resp = adapter.Handler._resp(None, "hello world")  # type: ignore[misc]
check("_resp shape: output text + status", resp["output"][0]["content"][0]["text"] == "hello world"
      and resp["status"] == "completed")
check("_resp usage totals consistent",
      resp["usage"]["total_tokens"] == resp["usage"]["input_tokens"] + resp["usage"]["output_tokens"])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
