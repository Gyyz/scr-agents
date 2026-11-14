#!/usr/bin/env sh
# Installs https://github.com/mrkrsl/web-search-mcp into ./server next to this script and builds it.
# No API key is needed: it searches Bing / Brave / DuckDuckGo directly. Needs Node >= 18 and npm.
# Pass --browsers to also install the Playwright Chromium build (better Bing results; without it the
# server falls back to the plain HTTP engines). Then enable the server in the console (Configure ->
# MCP servers) or in mcp_servers/config.yaml, and check it with: scr-agents mcp check --query "CWE-78"
set -eu
here=$(cd "$(dirname "$0")" && pwd)
dest="$here/server"
repo="https://github.com/mrkrsl/web-search-mcp.git"
if [ -d "$dest/.git" ]; then
    echo "updating $dest"
    git -C "$dest" pull --ff-only --quiet || echo "pull failed, keeping the current checkout"
else
    git clone --depth 1 "$repo" "$dest"
fi
cd "$dest"
npm install --no-audit --no-fund
npm run build
if [ "${1:-}" = "--browsers" ]; then
    npx playwright install chromium
fi
echo
echo "installed: $dest/dist/index.js"
echo "enable it in the console (Configure -> MCP servers) or in mcp_servers/config.yaml"
