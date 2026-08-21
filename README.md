# web-search-proxy (Exa-backed MCP server)

## What it is

An MCP stdio server giving AI harnesses three tools — `web_search`, `web_fetch`,
and `key_status` — backed by **Exa's hosted MCP endpoint**
(`https://mcp.exa.ai/mcp`). Same mechanism opencode uses for its built-in free
websearch: a JSON-RPC `tools/call` POST, reply parsed from direct JSON or SSE
(see `packages/core/src/tool/websearch.ts` in the opencode source).

**v2.1 (2026-08-22) added an API-key pool with rotation** (v2 was keyless-only;
the original v1 forwarded to a dead local service on :8318):

- Keys live in `~/.config/web-search-proxy/env` (chmod 600):
  `EXA_API_KEYS=key1,key2,key3,...` (singular `EXA_API_KEY=` also accepted).
  Env var `EXA_API_KEYS` overrides the file if set.
- Round-robin rotation; state persisted to `exa_keys.json` next to the script
  (per-key status/retry_at/ok/fail/recovered counters + `_cursor`).
- A key is usable iff `now >= retry_at`. Failure classification benches keys:

  | Signal | Meaning | Benched for |
  | --- | --- | --- |
  | embedded or HTTP `401` | invalid key | 24 h |
  | embedded or HTTP `402` (`NO_MORE_CREDITS` / budget tags) | monthly ~$10 pot empty | 1 h probe cycle |
  | `429` | per-second QPS limit (10 QPS on /search) | 15 s, key stays healthy |
  | network / other | transient | 60 s |

- **Exa hides API errors inside HTTP-200 MCP replies** as text like
  `web_search_exa error (401): Invalid API key` with `isError: true`. The
  classifier regexes `error \((\d{3})\)` out of the content text; HTTP-level
  statuses are handled as fallback.
- When no key is usable, requests transparently fall back to the **keyless**
  endpoint (free tier); results get prefixed
  `[all API keys unavailable - served keyless]`.
- **Refill detection is probe-based**: Exa has NO balance/credits API
  (verified: `/admin/stats`, `/admin/api-keys`, `/key/credits` all 404; docs
  only point at dashboard.exa.ai). Exhausted keys are retried once per
  `PROBE_INTERVAL` (1 h) when traffic flows; first success reactivates the key,
  bumps `refill_recoveries`, and prefixes results with
  `[key <masked> recovered - credits refilled]`. Exa's free tier adds $10 of
  credits every month, so keys re-enable automatically within ≤1 h of refill.
- Non-obvious gotcha (still true): Exa's edge 403s bare Python-urllib UA; the
  script sends `User-Agent: web-search-proxy/2.1`. Keep an explicit UA.

## Native web_search integration (exa_search_adapter.py + systemd)

Grok Build's **built-in** `web_search` tool is model-backed: `[models].web_search`
names a model, and Grok calls it on `POST {base_url}/responses` (OpenAI Responses
API) with `{"input": "<query>", "tools":[{"type":"web_search",...}]}` — discovered
empirically with a logging mock + MITM capture against the real backend.

`exa_search_adapter.py` implements that endpoint locally and answers every query
from the Exa rotation engine (`call_with_rotation` imported from
`mcp_web_search.py`, so key pool/fallback/state are shared).

- Listens on `127.0.0.1:8390` (`EXA_ADAPTER_PORT` env overrides).
- Replies in the exact Responses-API shape Grok accepts — including
  `completed_at`, `logprobs`/`annotations` inside content items; a minimal
  reply gets rejected (`exec_done success:false` in `~/.grok/logs/unified.jsonl`)
  and the agent silently falls back to the MCP server.
- Wired in `~/.grok/config.toml`:
  ```toml
  [models]
  web_search = "exa-search-local"        # was "grok-4.5"
  [model.exa-search-local]
  base_url = "http://127.0.0.1:8390/v1"
  api_backend = "responses"
  ```
- Runs as a **systemd user service**:
  ```bash
  systemctl --user status|restart|stop exa-search-adapter.service
  journalctl --user -u exa-search-adapter.service -f
  ```
  Unit: `~/.config/systemd/user/exa-search-adapter.service`; enabled,
  `Restart=always`, linger=yes (starts at boot).
- Adapter log: `~/.local/share/muse-filter/exa_adapter.log`.
- Config edits apply to NEW sessions only (config read at session start).
- Rollback: set `web_search = "grok-4.5"` in `[models]` (backup:
  `~/.grok/config.toml.bak-grok-exa-native-20260822`); stop service with
  `systemctl --user disable --now exa-search-adapter.service`.

## File map

| File | Purpose |
| --- | --- |
| `mcp_web_search.py` | MCP stdio server (entrypoint), v2.1 with key rotation |
| `exa_search_adapter.py` | Local Responses-API adapter powering NATIVE web_search via Exa |
| `~/.config/systemd/user/exa-search-adapter.service` | User service running the adapter on :8390 |
| `exa_adapter.log` | Adapter request log (queries + timings) |
| `mcp_web_search.py.bak-grok-exa-20260822` | Backup of pre-Exa version (:8318 forwarding design) |
| `exa_keys.json` | Runtime rotation state (safe to delete; recreated on next call) |
| `~/.config/web-search-proxy/env` | Key store, chmod 600 (`EXA_API_KEYS=k1,k2,...`) |
| `proxy.py` | Unrelated local proxy on 127.0.0.1:8318 (not in the search path anymore) |
| `web_search_proxy.py` | Older experimental variant, unused |

## Upstream protocol

```
POST https://mcp.exa.ai/mcp?exaApiKey=<KEY>        (or bare URL for keyless)
Content-Type: application/json
Accept: application/json, text/event-stream

{"jsonrpc":"2.0","id":1,"method":"tools/call",
 "params":{"name":"web_search_exa","arguments":{"query":"...","numResults":5,
             "type":"auto","livecrawl":"fallback"}}}
```

- `web_fetch` maps to Exa tool `web_fetch_exa` with `{urls:[url], maxCharacters:8000}`.
- Text lives at `result.content[].text`; check `isError` + embedded `(code)` there.
- Other Exa tools exist (`agent_run`, `web_search_advanced_exa`) but stay disabled.

## Service management

Not a daemon: spawned per-session by the harness over stdio.

Wired client — Grok Build, in `~/.grok/config.toml`:

```toml
[mcp_servers.web-search-proxy]
command = "/usr/bin/python3"
args = ["/home/wraient/.local/share/muse-filter/mcp_web_search.py"]
```

The harness auto-respawns on kill; script edits take effect on next spawn.

## Endpoints / interfaces

- stdin/stdout: newline-delimited JSON-RPC 2.0 (MCP protocol `2024-11-05`).
- Methods: `initialize`, `notifications/initialized`, `ping`, `tools/list`,
  `tools/call`.
- Tools:
  - `web_search {query, max_results?=5}`
  - `web_fetch {url}`
  - `key_status {}` — masked per-key status, counters, next-retry times
- Outbound: HTTPS POST to `https://mcp.exa.ai/mcp` only.

## Troubleshooting

1. **Everything says `[all API keys unavailable - served keyless]`**
   All keys benched. Inspect: call the `key_status` tool, or
   ```bash
   python3 -c "import json;print(json.dumps(json.load(open('$HOME/.local/share/muse-filter/exa_keys.json')),indent=2))" | sed 's/[0-9a-f]\{8\}[0-9a-f-]*[0-9a-f]\{4\}/<masked>/g'
   ```
   `402` entries clear themselves hourly (probe) / at monthly refill; `401`
   entries mean a dead key — remove it from the env file.
2. **Add / replace keys**: edit `~/.config/web-search-proxy/env`
   (`EXA_API_KEYS=k1,k2,k3`), then `pkill -f mcp_web_search.py` so sessions
   respawn and pick up the new pool. Never commit or paste keys in logs.
3. **Searches return `error: HTTP Error 403: Forbidden`**
   UA header lost. Verify edge still accepts our UA:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -X POST https://mcp.exa.ai/mcp \
     -H 'User-Agent: web-search-proxy/2.1' -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
   ```
   Expect `200`; else switch to any browser-style UA.
4. **Stale process after edits**
   ```bash
   pkill -f mcp_web_search.py   # harness respawns fresh within seconds
   ```
   (`pgrep -af "[m]cp_web_search"` to list without self-matching.)
5. **Totally dead (network/DNS)**
   ```bash
   curl -sS -m 10 https://mcp.exa.ai/mcp -X POST -H 'User-Agent: probe' \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | head -c 200
   ```
6. **Syntax check after editing**
   ```bash
   python3 -m py_compile /home/wraient/.local/share/muse-filter/mcp_web_search.py
   ```

## Rebuild / reinstall steps

1. Restore backup (reverts to broken :8318 design — not recommended):
   `cp mcp_web_search.py.bak-grok-exa-20260822 mcp_web_search.py`.
2. Recreate from scratch: MCP stdio server speaking newline-delimited JSON-RPC,
   tools above, POSTing `tools/call` to `https://mcp.exa.ai/mcp[?exaApiKey=…]`
   with explicit User-Agent; parse JSON-or-SSE for `result.content[].text`;
   classify embedded `error (NNN)` codes per the table; persist rotation state
   to JSON; keep keyless as last resort.
3. Re-check the `[mcp_servers.web-search-proxy]` block in `~/.grok/config.toml`.

## Smoke test

```bash
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"web_search","arguments":{"query":"kernel.org latest stable","max_results":2}}}' \
 '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"key_status","arguments":{}}}' \
 | python3 /home/wraient/.local/share/muse-filter/mcp_web_search.py
```

## Verification log

Verified working 2026-08-22:

- Pool expanded to 4 keys (~$40/month combined); each new key validated with a
  live 1-result search before joining (`82720666…`, `139b77e0…`, `ee8a667f…` OK).
- Round-robin confirmed through live harness calls: 4 consecutive searches
  distributed 1-per-key, cursor wrapped correctly.
- Keyed happy path: search via real key (`ok` counter incremented), clean
  markdown fetch, empty-query error line.
- Invalid-key drill (bogus UUID): embedded `error (401)` detected → key benched
  24 h with visible "next usage available at", request still answered via
  keyless fallback.
- Exhausted-key drill (state seeded `retry_at=now+300`): cooling key skipped,
  keyless served, status shows countdown.
- Refill drill (`retry_at` set to past): next search probed the exhausted key,
  succeeded → auto re-enabled, `refill_recoveries` bumped, user-visible
  "[key … recovered - credits refilled]" prefix.
- Live harness calls (`web-search-proxy__web_search`) confirmed serving through
  the keyed route after respawn. `key_status` verified over stdio; it appears in
  the harness tool registry from the next session (registry is built at spawn).
- **Native web_search via adapter (2026-08-22):** headless Grok sessions with
  `--tools "web_search"` and with the full default toolset both answered current
  questions ("latest stable Debian", "PlayStation maker", "Go latest version")
  with sources; adapter log + key counters (`ok` increments) prove requests
  served by the Exa pool. Contract captured from a real xAI backend-search
  reply before replicating its exact response shape.
