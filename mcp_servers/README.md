# MCP servers

Step-by-step guide with examples and troubleshooting: `docs/scr_agents/extending.md` (in the built docs: SCR Agents, Extending).

Every MCP server scr_agents can use lives in its own folder here, described by an `mcp.yaml`:

```
mcp_servers/
  config.yaml              which servers are on (written by the console; editing by hand is fine)
  web-search/              Bing / Brave / DuckDuckGo search, no API key (mrkrsl/web-search-mcp)
    mcp.yaml
    install.sh             clones and builds the server into ./server (gitignored)
    quiet-stdout.mjs       keeps the server's console.log off the MCP channel
  brave-search/            Brave Search API; needs BRAVE_API_KEY in the environment
    mcp.yaml
  <your-server>/mcp.yaml
```

## Turning a server on

1. Install what it needs (the console shows what is missing and the `install` line of its manifest):
   `sh mcp_servers/web-search/install.sh` for the web search server.
2. In the console: **Configure** (top left) -> **MCP servers**. Switch the server on, tick the agents
   that may use it (bug, security and style reviewer, validator), and press **Test** to start it
   and list its tools. A server with a `web_search` section can be made the agents' web search.
3. Changes are written to `mcp_servers/config.yaml` and apply to reviews started afterwards.

From a terminal: `uv run scr-agents mcp check --query "CWE-78 OS command injection"` lists every
folder here (on, off, not ready), starts the servers that are on, lists their tools and runs one
search through the allowlist.

## Adding a server

Make a folder with an `mcp.yaml`:

```yaml
name: cvedb                        # tool prefix: the agents see mcp_cvedb_<tool>
description: CVE lookups by product and version
homepage: https://example.org/cve-mcp
command: uvx                       # a stdio server: command + arguments
args: ["some-cve-mcp-server", "--data", "{dir}/data"]   # {dir} = this folder (absolute)
env: {CVE_MIRROR: "https://mirror.internal"}             # passed to the server, nothing else is
env_from_host: [CVE_API_KEY]       # copied from scr_agents's own environment when set; keys never go here
requires: ["{dir}/data"]           # files that must exist, else "not ready" and it is not started
requires_env: [CVE_API_KEY]        # environment variables that must be set
untrusted: true                    # default: results are wrapped as untrusted data
result_format: auto                # default: each reply's format is recognised (see below)
install: "uv tool install some-cve-mcp-server"           # shown when something is missing
# optional: lets the console make this server the agents' web_search tool
# web_search: {tool_name: search, query_arg: query, limit_arg: limit}
```

New folders start **off**; nothing in this directory runs until someone switches it on.

## Who can change what

The console edits only `mcp_servers/config.yaml`: on/off, which agents get the tools, which server backs
web search. It cannot change a command, its arguments or its environment: those come from the
`mcp.yaml` files on disk, written by whoever runs the server. The web search allowlist
(`web_search.allowlist`) also stays in `scr_agents.yaml`.

## How the agents use them

- Each tool of a server that is on becomes `mcp_<server>_<tool>` for the agents ticked for it
  (all four when none is ticked). The planner and the chat lead agent never get MCP tools.
- The web search server is offered only through the `web_search` wrapper: results are filtered to
  `web_search.allowlist` and sanitised, and the server's other tools stay hidden.
- Every result passes through the safety filter as untrusted text. Tool schemas are flattened
  (`$ref`/`$defs` inlined) so local vLLM / SGLang tool parsers accept them.
- Replies can be in any format: JSON, CSV, TSV, Markdown, HTML, XML or plain text, as text items or
  embedded resources. Each is recognised from its MIME type or its content; set `result_format` in
  `mcp.yaml` only when a server's replies are mistaken for another format. HTML reaches the agents
  as plain text with its links kept (scripts, styles and comments dropped). Images, audio and other
  binary content reach them as a one-line summary (type, size), never as data. The web search
  wrapper reads results from JSON, CSV/TSV, a Markdown table or link list, or numbered text.
- Servers start with each review and stop at its end. The console's MCP panel shows which servers a
  run had and every call, with the server's raw reply.

Servers can also be listed under `mcp_servers` in `scr_agents.yaml`; those are always on (optionally
limited with `components: [...]`) and the console shows them read-only.

## Docker

The image does not include Node. Build with `docker compose build --build-arg WITH_WEB_SEARCH=1` to
install the web search server into the image (`/app/mcp_servers/web-search/server`).
