#!/usr/bin/env bash
# Installs the Exa-backed web search stack for Grok Build:
#   1. MCP stdio server (web_search / web_fetch / key_status) with API-key rotation
#   2. Local Responses-API adapter making Grok Build's NATIVE web_search run on Exa
#   3. systemd user service for the adapter
# Keys are NOT included: put them in ~/.config/web-search-proxy/env (see env.example).
set -euo pipefail

MUSE_DIR="$HOME/.local/share/muse-filter"
ENV_DIR="$HOME/.config/web-search-proxy"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$MUSE_DIR" "$ENV_DIR"
install -m 755 "$SCRIPT_DIR/mcp_web_search.py" "$MUSE_DIR/"
install -m 755 "$SCRIPT_DIR/exa_search_adapter.py" "$MUSE_DIR/"

if [ ! -f "$ENV_DIR/env" ]; then
    install -m 600 "$SCRIPT_DIR/env.example" "$ENV_DIR/env"
    echo ">> Created $ENV_DIR/env - EDIT IT and set EXA_API_KEYS=key1,key2,..."
else
    echo ">> Keeping existing $ENV_DIR/env"
fi

mkdir -p "$HOME/.config/systemd/user"
install -m 644 "$SCRIPT_DIR/systemd/exa-search-adapter.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now exa-search-adapter.service
sleep 1
systemctl --user is-active exa-search-adapter.service

cat <<'EOF'

>> Next step (manual): add to ~/.grok/config.toml

[models]
web_search = "exa-search-local"        # in your existing [models] block

[model.exa-search-local]
model = "exa-search-local"
base_url = "http://127.0.0.1:8390/v1"
name = "Exa Web Search (native)"
api_key = "local"
api_backend = "responses"
context_window = 128000

Then start a NEW Grok session. Smoke test:

printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"web_search","arguments":{"query":"kernel.org","max_results":2}}}' \
 | python3 ~/.local/share/muse-filter/mcp_web_search.py
EOF
