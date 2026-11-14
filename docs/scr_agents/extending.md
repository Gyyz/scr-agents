# Adding skills and MCP servers

scr_agents has two extension points that need no code changes:

- **Skills**: written guidance the agents read, such as a checklist for a language, your team's
  conventions, or how to judge evidence. A skill is a Markdown file.
- **MCP servers**: external tools the agents can call, such as web search, a CVE database or an
  internal API. A server is a program scr_agents starts for each review.

|                    | Skills | MCP servers |
|--------------------|--------|-------------|
| Lives in           | `src/scr_agents/skills/<folder>/<name>/SKILL.md` | `mcp_servers/<name>/mcp.yaml` (plus the server's code, if any) |
| On when added      | **yes** | **no**: switch it on first |
| Switched in        | console: **⚙ Configure → Skills** | console: **⚙ Configure → MCP servers** |
| Stored in          | `src/scr_agents/skills/config.yaml` | `mcp_servers/config.yaml` |
| Takes effect       | next review; console chats at once | next review |
| Check from a shell | `uv run scr-agents skills` | `uv run scr-agents mcp check` |
| Details            | `src/scr_agents/skills/README.md` | `mcp_servers/README.md` |

Both `config.yaml` files are console state and are gitignored. Remove their lines from `.gitignore`
if you want to version which skills and servers are on.

---

## Part 1: Skills

### 1. Pick the folder

The folder decides which agents see the skill. Each agent sees `shared_skills/` plus its own folder,
never another agent's folder.

| Folder | Who reads it | How it reaches the agent |
|---|---|---|
| `shared_skills/` | every agent below | as for each agent; filtered by `languages` and `categories` |
| `planner/` | the planner (picks focus files, writes hypotheses) | full text in its prompt (it has no tools), about 6,000 characters in total |
| `orchestrator/` | the OpenFlow orchestrator (`review.orchestrator: openflow`) | `skill_list` / `skill_read` |
| `bugs/` | the bug reviewer | name and description in its task message; full text with the `skill_read` tool |
| `vulnerabilities/` | the security reviewer | same |
| `style/` | the style reviewer | same |
| `validator/` | the validator (the second model, which runs proofs in the sandbox) | named in each finding's task message; `skill_read` |
| `lead/` | the chat lead agent | `skill_list` / `skill_read` when it needs them |

Rule of thumb: **domain knowledge** (a language, a framework, a class of weakness) goes in
`shared_skills/` with `languages` / `categories` set. **How to do one agent's job** goes in that
agent's folder.

### 2. Write `SKILL.md`

```bash
mkdir -p src/scr_agents/skills/shared_skills/django-auth
$EDITOR src/scr_agents/skills/shared_skills/django-auth/SKILL.md
```

```markdown
---
name: django-auth
description: Authentication and authorisation checks for Django views and DRF endpoints
languages: [python]
categories: [vulnerability, bug]
---

# Django authentication and authorisation

## Check on every new or changed view
- `@login_required`, `LoginRequiredMixin` or DRF `permission_classes` present ...

## How to confirm
- Call the view with the test client as an anonymous user and as another user ...
```

| Field | Required | Meaning |
|---|---|---|
| `name` | no (defaults to the folder name) | Unique across all skills. Use lowercase letters, digits and dashes: the console cannot switch names with spaces. |
| `description` | yes, in practice | One line. It is all an agent sees until it decides to read the skill, so say what it covers. |
| `languages` | no | Offer the skill only when the change touches these: `python`, `c`, `cpp`, `go`, `rust`, `javascript`, `typescript`, `java`, `kotlin`, `ruby`, `php`, `csharp`, `swift`, `shell`. Empty means every language. |
| `categories` | no | Offer it only to these reviewers, and to the validator for findings of these categories: `bug`, `vulnerability`, `style`, `naming`, `performance`. Empty means all. |

A file without front matter also works; it is then named after its folder and has no description.

### 3. Write guidance the models can use

- **Be concrete.** Name the functions, patterns and inputs to look for ("`yaml.load` without
  `Loader=SafeLoader`"), not "be careful with YAML".
- **Say how to confirm.** The validator turns a finding into a verdict by running code: tell it
  what to run and what output proves the problem.
- **Say what not to report.** False positives cost more than misses. List the patterns that look
  bad but are fine in your codebase.
- **Keep it short.** A local 31B or 70B model follows 50 focused lines better than 500.
  Planner skills share a 6,000-character budget.
- **Point to tools.** "Look up the CWE with `web_search` before reporting" steers an agent to an
  MCP tool it would otherwise ignore.

The starter skills are worked examples: `src/scr_agents/skills/validator/proof-discipline/SKILL.md` and
`src/scr_agents/skills/shared_skills/python-injection/SKILL.md`.

### 4. Load and check it

1. **Console:** **⚙ Configure → Skills → Reload**. The skill appears under its folder with a switch
   and a **View** button; parse errors are listed at the bottom.
2. **Terminal:** `uv run scr-agents skills` lists every skill per folder (on or off, languages,
   categories) and prints files that failed to parse.

New skills are **on**. Reviews started afterwards use them. A chat in the console picks them up at
once; a terminal `scr-agents chat` loads skills when it starts.

### 5. See that an agent used it

In a review's run card, open **Agent steps**. A `skill_read(name=…)` call shows that an agent read
the skill. For the full prompt, click an agent's model call and open **Show the full request and
response**: the task message lists the skills it was offered, and the planner's message contains
its own skills inline.

### 6. Switch skills off and on

Use the switch in **⚙ Configure → Skills**, or edit `src/scr_agents/skills/config.yaml` by hand:

```yaml
disabled: [actionable-style, c-memory-safety]
```

To remove a skill completely, delete its folder.

### 7. Skills from someone else's repository

Copy each skill folder so that `SKILL.md` sits exactly at `src/scr_agents/skills/<folder>/<name>/SKILL.md`:

```bash
git clone --depth 1 https://github.com/someone/review-skills /tmp/review-skills
cp -R /tmp/review-skills/skills/go-concurrency src/scr_agents/skills/bugs/
uv run scr-agents skills        # check it loaded under bugs/
```

A clone dropped in as-is usually nests its skills deeper (`src/scr_agents/skills/shared_skills/review-skills/skills/x/`).
That folder has no `SKILL.md` at the expected depth, so it is reported, not loaded.

**Read a third-party skill before adding it.** Skills go into the prompts as trusted operator
content, like the system prompts, so a hostile skill can steer the agents.

### 8. Rules

- Skills load only from `skills.paths` (default `builtin`, the package's `src/scr_agents/skills/`), never from the
  repository under review.
- Extra roots with the same layout can be added: `skills: {paths: [builtin, /srv/team-skills]}`. When
  two skills share a name, the first one found wins and the clash is reported.
- No secrets in skills: they are sent to the model with every matching review.
- A skill placed directly in `src/scr_agents/skills/<name>/` (the older flat layout) counts as shared.

---

## Part 2: MCP servers

### What happens to a server

- It starts at the beginning of each review it is on for, and stops at the end.
- Each of its tools becomes an agent tool named `mcp_<server>_<tool>`, offered to the agents ticked
  for it (bug, security and style reviewer, validator, OpenFlow orchestrator; all of them when none is
  ticked). The planner and the chat lead agent never get MCP tools.
- Every result passes through the safety filter as untrusted data: secrets are redacted,
  injection attempts flagged, and the text wrapped so the model treats it as data.
- The server can reply in any format: JSON, CSV, TSV, Markdown, HTML, XML or plain text. Each reply
  is recognised from its MIME type or its content, and the console shows it that way (a table for
  CSV, rendered Markdown, HTML as source). Agents read HTML as plain text with its links kept, and
  images, audio or other binary content as a one-line summary, never the data itself.
- **The server runs as a normal process with scr_agents's own user and network access. It is not
  sandboxed.** Only add servers whose code you trust.

### 1. Choose the kind of server

**a. A published package (npm, PyPI): write only a manifest.**

```bash
mkdir mcp_servers/fetch
cat > mcp_servers/fetch/mcp.yaml <<'EOF'
name: fetch
description: Fetch a web page and return it as Markdown
homepage: https://github.com/modelcontextprotocol/servers/tree/main/src/fetch
command: uvx
args: ["mcp-server-fetch"]
install: "needs uv (uvx); the package downloads on first start"
EOF
```

`mcp_servers/brave-search/mcp.yaml` is the npm version of this (`command: npx`).

**b. A repository you clone and build: clone into `server/`.**

```bash
mkdir mcp_servers/cvedb
git clone --depth 1 https://github.com/example/cve-mcp mcp_servers/cvedb/server   # gitignored
(cd mcp_servers/cvedb/server && npm install && npm run build)
cat > mcp_servers/cvedb/mcp.yaml <<'EOF'
name: cvedb
description: CVE lookups by product and version
command: node
args: ["--import", "{dir}/../web-search/quiet-stdout.mjs", "{dir}/server/dist/index.js"]
requires: ["{dir}/server/dist/index.js"]
install: "cd mcp_servers/cvedb/server && npm install && npm run build"
EOF
```

Cloning into `mcp_servers/<name>/server/` keeps the clone and its `node_modules` out of git (`mcp_servers/*/server/`
is ignored) and leaves room for the manifest and an install script beside it. `mcp_servers/web-search/` is
the full example, with `install.sh`.

**c. A small server of your own: copy the test server.**

`tests/mcp_echo_server.py` is a complete stdio server in about 30 lines of Python (`@mcp.tool()`
functions). Copy it to `mcp_servers/<name>/server.py`, then:

```yaml
name: inventory
description: Look up which services use a library version (internal inventory API)
command: python3
args: ["{dir}/server.py"]
env_from_host: [INVENTORY_TOKEN]
requires_env: [INVENTORY_TOKEN]
```

The interpreter must have the `mcp` package installed. Use the scr_agents venv's python
(`.venv/bin/python`) or install it with `pip install mcp`.

### 2. `mcp.yaml` reference

| Field | Meaning |
|---|---|
| `name` | Required. Letters, digits, `-`, `_`, at most 40 characters, starting with a letter. Becomes the tool prefix (`mcp_<name>_<tool>`) and the key in `mcp_servers/config.yaml`. Must be unique. |
| `description` | One line for the console. |
| `homepage` | Link shown in the console. |
| `command` | Required. The executable: a name on `PATH` (`node`, `npx`, `uvx`, `python3`) or a path. |
| `args` | Arguments. `{dir}` is replaced by this folder's absolute path, so the server works from any working directory. |
| `env` | Environment for the server, nothing else is passed to it. `{dir}` works here too. Do not put keys here. |
| `env_from_host` | Names of variables copied from scr_agents's own environment when set: this is where keys come from (`export BRAVE_API_KEY=…` before `scr-agents serve`). |
| `requires` | Files that must exist (`{dir}` allowed). If one is missing, the server is **not ready** and is not started. |
| `requires_env` | Environment variables that must be set; if not, the server is **not ready**. |
| `untrusted` | Default `true`: results are wrapped as untrusted data. Keep it. |
| `result_format` | Default `auto`: each reply's format is recognised from its MIME type or content. Set `json`, `csv`, `tsv`, `markdown`, `html`, `xml` or `text` only when replies are mistaken for another format (for example prose with one comma per line read as CSV). |
| `install` | How to install it. Shown in the console when something is missing; scr_agents never runs it. |
| `web_search` | Optional `{tool_name, query_arg, limit_arg}`. Lets the console make this server the agents' `web_search` tool (see step 4). |

A server is **ready** when its command is found and its `requires` and `requires_env` are met. The
console and `scr-agents mcp check` say exactly what is missing.

### 3. Switch it on

1. Open **⚙ Configure → MCP servers** (press **Reload** if the console was already open). The new
   folder is listed, **off**, with its command and its status.
2. If it says **not ready**, fix what it lists (install the runtime, build, export the key), then
   press **Reload**.
3. Press **Test**. scr_agents starts the server as a review would and lists its tools, or shows the
   error.
4. Turn on the switch and tick the agents that should get its tools. A CVE database suits the
   security reviewer and the validator; a style guide service suits the style reviewer.

Or edit `mcp_servers/config.yaml` by hand:

```yaml
servers:
  cvedb:
    enabled: true
    components: [vulnerabilities, validator]   # empty or missing = all four agents
web_search: websearch                          # the server behind web_search, or null
```

The console can change only these switches. A server's command, arguments and environment come
only from its `mcp.yaml`, so they cannot be changed from the browser.

### 4. Web search

One server can back the agents' `web_search` tool. That wrapper keeps only results from the domains
in `web_search.allowlist` in `scr_agents.yaml`, and hides the server's other tools. To use it:

1. Give the manifest a `web_search` section naming the server's search tool and its parameters,
   e.g. `{tool_name: get-web-search-summaries, query_arg: query, limit_arg: limit}`.
2. Switch the server on, then pick it under **Web search** in **⚙ Configure → MCP servers**.
3. Add the documentation sites your reviewers should be able to cite to `web_search.allowlist` in
   `scr_agents.yaml`. An empty allowlist drops every result.

Check it from a terminal: `uv run scr-agents mcp check --query "CWE-78 OS command injection"`
prints what a query returns after the allowlist filter.

### 5. Watch it work

During a review, the console's **MCP** panel (right side) lists the servers the run started and
every call: the arguments sent, the server's raw reply, and what the agent received after
filtering. Click a call for the full JSON-RPC request and reply. Models call tools only when they
think they need them; if a useful tool is never called, a skill that says when to use it helps.

### 6. Servers in Docker

The image does not contain what you installed on your own machine (`mcp_servers/*/server/` is in
`.dockerignore`, because a macOS build does not run in a Linux image). The web search server can be
built into the image with `docker compose build --build-arg WITH_WEB_SEARCH=1`. For other servers,
add their runtime and install step to `deploy/Dockerfile`, next to the `WITH_WEB_SEARCH` block, and
keep `{dir}` in the manifest so paths resolve inside `/app/mcp_servers/`.

### 7. Troubleshooting

| What you see | Cause and fix |
|---|---|
| Not listed in the console | `mcp.yaml` has a YAML error or a bad `name`: the error is listed at the bottom of the MCP tab, and in `scr-agents mcp check`. |
| `not ready: command 'node' not found` | Install the runtime on the machine that runs scr_agents (and in the image). |
| `not ready: file …/dist/index.js` | Build the server (its `install` line says how). |
| `not ready: environment variable X` | `export X=…` in the shell that starts `scr-agents serve`, then restart it. |
| Test: `did not start` / timeout | Run the command from the card by hand; most servers print why. Check the arguments and the `{dir}` paths. |
| Test works but there are parse warnings in the log | The server logs to stdout, the MCP channel. Node servers: start through `--import {dir}/../web-search/quiet-stdout.mjs`; others usually have a quiet flag. |
| Switched on, but no `mcp_<name>_…` tools in a review | The agent is not ticked for it, or the server failed to start: see the run's MCP panel (failed servers show their error). |
| A server from `scr_agents.yaml` hides a folder | `mcp_servers` in the config file wins a name clash; rename one of them or delete the config entry. |
| The tests start your server | They should not: `tests/conftest.py` points `SCR_AGENTS_MCP_SERVERS_DIR` at an empty folder. |

### 8. Alternative: define a server in the config file

```yaml
mcp_servers:
  - name: cvedb
    command: uvx
    args: ["some-cve-mcp-server"]
    components: [vulnerabilities, validator]
```

Servers defined there are always on and show read-only in the console. Use this for a fixed
deployment; use `mcp_servers/` when people should switch servers from the console.

---

## Checklist

**Skill**
- [ ] `SKILL.md` at `src/scr_agents/skills/<folder>/<name>/SKILL.md`, with a one-line `description`
- [ ] `languages` / `categories` set when it is not universal
- [ ] Concrete checks, how to confirm, what not to report; no secrets
- [ ] **Reload** in the console, or `scr-agents skills`, shows it with no errors

**MCP server**
- [ ] Code reviewed and trusted (it runs unsandboxed)
- [ ] `mcp_servers/<name>/mcp.yaml` with `name`, `command`, `args` (using `{dir}`), `requires`, `install`
- [ ] Keys passed through `env_from_host`, never written in the file
- [ ] **Test** in the console lists its tools
- [ ] Switched on for the right agents; `web_search` section and allowlist if it is a search server
