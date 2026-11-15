# Overview

A self-hosted, multi-agent code review system. Give it a git repository and two commits
(optionally a pull request) and it produces an evidence-backed report of bugs, vulnerabilities
and style problems, with every claim re-checked by a second model that can build and run both
commits in a sandbox.

Extra tools reach the agents as MCP servers started from `mcp_servers/`, and review guidance lives
in `src/scr_agents/skills/`. New here? Read the {doc}`handover` (current state, how to run, deployment,
event protocol, extension points).

## Architecture to code

| Diagram box | Workstream | Module |
|---|---|---|
| Review request: code diff, repo and history, PR and comments, commit messages | input | `scr_agents/inputs/` |
| Code graph (change blast radius) | WS2 | `scr_agents/ws2_analysis/code_graph.py` |
| Static scanners (SAST, linters, style) | WS2 | `scr_agents/ws2_analysis/scanners.py` |
| Safety filter (everything untrusted passes here) | WS4 | `scr_agents/ws4_guardrails/safety_filter.py` |
| Web search (MCP, allowlisted sites) | WS4 | `scr_agents/ws4_guardrails/web_search.py`, `scr_agents/llm/mcp_client.py` |
| Approved registry (signed, A/B tested) | WS4+WS1 | `scr_agents/ws4_guardrails/registry.py`, `scr_agents/prompts/manifest.yaml` |
| Review agent (orchestrates subagents) | WS1 | `scr_agents/ws1_review/orchestrator.py` (deterministic), `openflow.py` (an LLM orchestrates; `review.orchestrator: openflow`) |
| Bugs / Vulnerabilities / Style subagents | WS1 | `scr_agents/ws1_review/subagents.py`, `tools.py` |
| Validator agent (second model re-checks) | WS1 | `scr_agents/ws1_review/validator.py` |
| QEMU test bench (builds both commits) | WS3 | `scr_agents/ws3_harness/test_bench.py`, `workspace.py` |
| Shell terminal (sandboxed commands) | WS3 | `scr_agents/ws3_harness/shell.py` |
| Evidence report (proof of exploit, fixes) | WS3 | `scr_agents/ws3_harness/evidence.py` |
| Knowledge base (CWE, CERT C, MISRA) | WS5 | seed entries in `scr_agents/ws5_knowledge/knowledge_base.py` and `scr_agents/knowledge/*.yaml`; a larger knowledge base plugs in as an MCP server in `mcp_servers/` |
| Activity log (tagged with commit IDs) | WS5 | `scr_agents/ws5_knowledge/activity_log.py` |
| Human review (verifies early versions) | WS5 | `scr_agents/ws5_knowledge/human_review.py`, `scr-agents verdict` |
| LLM + tool loop shared by the agents | infra | `scr_agents/llm/backend.py`, `loop.py` |
| Wiring in diagram order | infra | `scr_agents/pipeline.py`, `cli.py` |

## Quick start

```bash
uv sync
uv run scr-agents demo             # plants defects in a temp repo and reviews it
```

By default the agents talk to a **locally hosted LLM** through the OpenAI-compatible API that vLLM
and SGLang expose. An online API is called only when you explicitly set `llm.mode: openai` or
`llm.mode: anthropic`; the code under review is then sent to that provider.

```bash
# 1. serve a model locally. Defaults prefer Gemma 4 31B, then Llama 3.x 70B (llm.local.preferred_models)
vllm serve google/gemma-4-31B-it --port 8000 \
    --enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4 \
    --chat-template examples/tool_chat_template_gemma4.jinja        # vLLM >= 0.19
vllm serve meta-llama/Llama-3.3-70B-Instruct --port 8000 \
    --enable-auto-tool-choice --tool-call-parser llama3_json \
    --chat-template examples/tool_chat_template_llama3.1_json.jinja
python -m sglang.launch_server --model-path meta-llama/Llama-3.3-70B-Instruct --port 8000 \
    --tool-call-parser llama3
# optional, both engines: report prefix (KV) cache hits per call, shown in the console's Memory panel:
#   vLLM --enable-prompt-tokens-details   SGLang --enable-cache-report

# 2. verify tool calling and JSON output work through that server
uv run scr-agents llm check                 # uses llm.local.base_url (default http://127.0.0.1:8000/v1)

# 3. review
uv run scr-agents review --repo /path/to/repo --base main --head HEAD
uv run scr-agents review --repo . --base v1.2.0 --head v1.2.1 --task verify_fix \
    --claim "fixes the heap overflow in parse_header (CVE-2026-XXXX)" --pr gh:42
```

The tool-call and reasoning parser names depend on the model; take them from the model card or
the vLLM / SGLang function-calling docs. Point `llm.local.validator_model` (and optionally
`validator_base_url`) at a second served model so the validator is independent of the reviewer.

Without a reachable local server the system runs in **offline mode**: a deterministic stand-in
drives the same agent loop, so scanners, code graph, test bench, validator evidence, report and
activity log all run end to end. This is what CI and the tests use.

Outputs land in `out/sessions/<session_id>/<run_id>/`: `report.html` (the detailed report, one self-contained
page), `report.md`, `report.json`, `activity.jsonl`, `request.json`, `human_review.md`. A session is one `chat` conversation, one
`serve` process, or one `review`/`demo` invocation (`scr-agents sessions` lists them). Record human
verdicts to close the feedback loop:

```bash
uv run scr-agents verdict --run run-xxxx --finding F-1234abcd --verdict accept --note "repro'd"
uv run scr-agents registry stats      # acceptance per prompt variant (A/B)
```

## How a review runs

1. **Inputs**: `build_request` resolves both commits, parses the unified diff into files and
   hunks, collects commit messages and (optionally) PR text via a JSON file or `gh pr view`.
2. **Workspace**: base and head are exported with `git archive` into plain directories under the
   run directory. They carry no `.git`, so nothing built or run there can reach the repository, its
   hooks or its config; host-side git itself runs with hooks and fsmonitor disabled.
3. **WS2**: the code graph maps changed lines to symbols and finds their callers (blast radius);
   scanners that are installed run on the changed files (ruff, bandit, semgrep, cppcheck, gosec),
   plus a built-in regex scanner so there is always signal.
4. **WS3**: the test bench detects or uses configured build/test commands and runs them on both
   commits through the sandboxed shell.
5. **WS4**: every untrusted text (diff, commits, PR, scanner output, tool results, web results)
   goes through the safety filter: invisible characters stripped, secrets redacted, injection
   patterns flagged, size capped, wrapped in `<untrusted>` tags.
6. **WS1**: the review agent plans, then runs the three subagents in parallel. Each is a tool-use
   loop with read-only tools that records findings through a `report_finding` tool (validated
   fields, so local models see the schema) and ends with a JSON summary. Reports of the same spot
   merge into one finding that remembers every source (agreement); scanner hits strengthen a
   finding or, when high-severity and unreported, become candidates; every candidate is grounded
   against the head checkout (path, line, nearness to the change) and ranked by grounding,
   agreement, severity, reproducibility and confidence.
7. **WS1 validator**: a second model (in local mode a second served model; the run warns when
   reviewer and validator resolve to the same one) re-checks each candidate with the harness tools.
   It sees the claim without the reviewer's confidence or reasoning, may write proof-of-concept
   files into a scratch directory, must cite lines or reproduce to confirm, and returns
   confirmed / rejected / unverified with evidence; its own `run_tests` and `run_command` calls are
   attached as evidence automatically.
8. **WS3/WS5**: the evidence report, human review packet and activity log are written.

## Interactive session (terminal, Claude Code style)

Point it at a local path, a git URL or `owner/repo` and talk to it:

```bash
uv run scr-agents chat /path/to/project
uv run scr-agents chat https://github.com/org/repo.git
```

```
› check the last 3 commits for memory safety problems
⏺ pipeline  review run-3f2a…: 5 files, 3 commits, task find_bugs
⏺ pipeline  graph done  symbols=9, impacted_files=4
⏺ pipeline  scanners done  findings=4, run=['builtin', 'cppcheck']
⏺ pipeline  bench done  delta=both pass
⏺ review_agent  plan: 2 focus files, 3 hypotheses
⏺ subagent:vulnerabilities  started (gemma-4-31B-it)
  ⏺ subagent:vulnerabilities  scanner_findings()
    ⎿  1.2k chars · 0.0s
  ⏺ subagent:vulnerabilities  read_file(path=src/parse.c, start_line=40, end_line=120)
    ⎿  3.1k chars · 0.0s
⏺ subagent:vulnerabilities  done · 2 findings · 4 turns
⏺ validator:F-91c0e2a1  run_command(argv=["./build/parse", "poc.bin"], ref=head)
⏺ validator:F-91c0e2a1  confirmed
╭ Findings for run-3f2a… · 1 confirmed, 1 unverified ─╮
│ F-91c0e2a1  confirmed  high  Heap overflow in parse_header  src/parse.c:88 │
…
```

The lead agent decides what to run (a commit range, the whole repository with scope `all`, or a
fix verification), passes your wording to the subagents as focus, and summarises the result. Then
drill in: `/show <id>` prints the evidence and fix, `/log` the full trace, `/web` opens the web
console for the same runs, `/verdict <id> accept|reject` records your judgement. `/help` lists
everything. Non-interactive: `scr-agents chat <target> --ask "review the whole project"`, or
`scr-agents review --repo <url> --base main --live` for the same live view without the chat.

## Web console

```bash
uv run scr-agents serve            # prints http://127.0.0.1:8765/?token=... - open that URL
```

Every `/api` route needs the access token: browsers get it once from the printed `?token=` URL
(exchanged for a SameSite cookie), API clients send `Authorization: Bearer <token>`. The token is
generated per start unless `web.token` or `SCR_AGENTS_TOKEN` is set. The console accepts git URLs
(`https` by default, `web.url_schemes`) and paths under `projects_dir`; other local paths need
`web.allow_local_paths: true`. The LLM mode is the server's configuration, never the client's.
For access by hostname set `web.allowed_hosts` (the default is loopback only; `serve --host
0.0.0.0` falls back to `*` with a warning).

The console is a **conversation** with the review agents. Paste a git URL (or, with
`web.allow_local_paths`, a folder path) and optionally what to check; the lead agent decides what to
run, starts reviews and answers follow-up questions in the same chat. Each review appears inline as a
compact card that fills in live (stage bar, the step being run, then the findings with their
verdicts). The agents' individual steps (subagents, validators, tool calls) are kept out of the way in a
small scrolling **Activity** list in the top-right corner. Click any row, finding or "Run details" to
open the detail drawer: tool inputs and outputs, model turns and token usage, the validator's evidence,
the suggested fix, and accept/reject for your verdict.

Every model API call and every tool call is **traceable**: each one writes a JSON trace next to the
activity log (`<run>/trace/`), with the full request (system prompt, messages, tools), the response,
and the exact HTTP request and response bodies exchanged with vLLM/SGLang (retries and JSON-repair
calls included; no headers or API keys). Tool traces hold the complete input and output. In the
console, open any step and press "Show the full request and response", or follow "raw JSON".
While a review runs, its card in the chat narrates the work as a checklist of plain-English
steps: *Reading the change → Building the code graph → Scanning for suspicious code → Building and
testing both commits → Planning the review → Analyzing the code* (with what each reviewer is doing
right now) *→ Filtering the findings* (raw findings, duplicates merged, scanner hits promoted) *→
Verifying each finding with a second model → Writing the report*. Each finished step turns into a
one-line summary of its result; when the review ends the steps fold away above the findings. The
lead agent's status line uses the same phrasing ("Reading app/utils.py…").

Running steps are highlighted in the Activity list.

Tool use is shown where it happens: every tool call of the lead agent appears inline in the chat as a
collapsible block (input, result, link to the full trace), and each run card has an **Agent steps**
section with the same blocks for every subagent and validator, grouped by agent. MCP calls are marked
with their server (`MCP · <server>`) and the server's own tool name. Above the Activity list, the
**Orchestrator** panel shows the overall progress of the current review: a progress bar, a sentence on
what is happening now, the phases with their results, the plan's hypotheses, each subagent's state and
findings, and the validation progress with the verdicts so far.

To delete history, hover a conversation or a past run in the sidebar and press ×. Deleting a
conversation removes its messages, the reviews it started, their reports and traces; the repository's
own project memory (`.scr_agents/`) is kept. Anything still running cannot be deleted until it
finishes. From the terminal: `scr-agents sessions delete <session-id|run-id> [--yes]`.

Each chat is a session (`out/sessions/<chat_id>/`, kind `chat-web`) whose activity log holds the
messages, the lead agent's steps and markers for the reviews it started, so chats replay after a
restart and resume when you send another message. Reviews started outside a chat are listed under
"Past runs". Chat API: `POST /api/chats/new {repo | demo, text?}`, `POST /api/chats/<id>/messages
{text}`, `GET /api/chats/<id>/events` (server-sent events; `?follow=0` returns the transcript and
closes), `GET /api/chats`.

## Project memory (`.scr_agents/` in the reviewed repository)

Every reviewed repository keeps its own memory in `.scr_agents/`, which you can commit to share
with the team: `memory.md` (notes the agents read before every review: how to build and test,
conventions, known false positives), `project.yaml` (overrides: bench commands, ignored paths and
scanner rules), `findings.jsonl` and `state.json` (history of every finding and run, with human
verdicts). The planner and subagents read the notes, the validator sees each finding's history
("a human rejected this before"), and reports mark findings that were seen before.

```bash
uv run scr-agents memory show --repo /path/to/repo
uv run scr-agents memory note --repo /path/to/repo --section "Build and test" "run make deps before pytest"
```

In a chat session: `/memory`, `/remember <text>`, or just tell the lead agent to remember something.

## Skills (`src/scr_agents/skills/`)

Skills are review guidance the operators write once and the agents pull in on demand: a
checklist for a language, a team's conventions, how to plan a review, what counts as proof. One
folder per skill with a `SKILL.md` (YAML front matter: `name`, `description`, optional `languages`
and `categories`; then Markdown), placed by audience:

- `src/scr_agents/skills/shared_skills/`: every agent (`c-memory-safety`, `python-injection`)
- `src/scr_agents/skills/planner/`, `orchestrator/`, `bugs/`, `vulnerabilities/`, `style/`, `validator/`, `lead/`: that agent only;
  each ships one starter skill

Reviewers, the validator, the OpenFlow orchestrator and the chat lead agent get `skill_list` and `skill_read` and their task
message names the applicable skills; the planner, which has no tools, gets its own skills inline.
Switch skills on and off in the console (**Configure** -> **Skills**). Skills load from
`skills.paths` (default `builtin`, this folder), never from the repository under review, so they count as
operator content like the prompts. `scr-agents skills` lists what is loaded; `src/scr_agents/skills/README.md` has
the format, and {doc}`extending` is the step-by-step guide to adding skills and MCP servers.

## MCP servers (`mcp_servers/` in this repository)

Each MCP server is a folder with an `mcp.yaml` that says how it starts: `mcp_servers/web-search/` (Bing,
Brave and DuckDuckGo, no key; `sh mcp_servers/web-search/install.sh` builds it) and `mcp_servers/brave-search/`
(needs `BRAVE_API_KEY`). New folders start off. In the console, **Configure** (top left) -> **MCP
servers** switches a server on, picks the agents that get its tools, makes one the agents' web
search and tests it; the command itself can only be changed in the file. `mcp_servers/README.md` explains
how to add a server (step by step: {doc}`extending`); `scr-agents mcp check --query "..."` does the same checks
from a terminal.

## Configuration

Copy `src/scr_agents/configs/default.yaml` to `scr_agents.yaml`, edit, and pass `--config`.
Highlights:

- `llm.mode`: `auto` | `local` | `openai` | `anthropic` | `offline`. `auto` picks `local` when the
  server in `llm.local.base_url` answers, otherwise `offline`; an online API is never used unless
  you set `openai` or `anthropic` explicitly.
- `llm.openai` (mode `openai`): `base_url` (OpenAI's API by default; any hosted OpenAI-compatible
  API works), `model` (required), `validator_model`, `api_key_env` (the environment variable that
  holds the key, `OPENAI_API_KEY` by default; the key itself never goes in a config file),
  `token_param` (`max_completion_tokens`, or `max_tokens` for older models and other providers),
  `temperature` (not sent unless set) and `context_window`. `scr-agents llm check` tests it.
- Mode `anthropic` uses `llm.reviewer_model` and `llm.validator_model` on the Claude API. It needs
  the opt-in extra (`uv sync --extra anthropic`) and `ANTHROPIC_API_KEY`.
- `llm.local`: `base_url`, `model` (empty = first model the server lists), `validator_model`,
  `temperature`, `max_tokens`, `json_repair` (re-ask with `response_format` guided decoding when
  the final answer is not JSON) and `extra_body` passed through to the server, e.g.
  `{chat_template_kwargs: {enable_thinking: false}}` to turn off thinking on Qwen3.
- `scanners.enabled`, `scanners.only_changed_lines`.
- `shell.allowed_binaries`: the only executables agents may run (argv only, no shells).
- `bench`: runner `local` (implemented), `docker` / `qemu` (placeholders), build/test commands.
- `mcp_servers_dir`: the folder of MCP servers (default `mcp_servers/`, see above; env `SCR_AGENTS_MCP_SERVERS_DIR`).
  `mcp_servers`: extra stdio servers defined in the config file, always on, optionally limited with
  `components: [bugs, vulnerabilities, style, validator]`. Tools become `mcp_<server>_<tool>`;
  results are filtered. `tests/mcp_echo_server.py` is a minimal server you can copy.
- `web_search.allowlist`: only results on these domains reach the agents, whichever server backs
  web search (chosen in the console, or with `web_search.mcp_server` for a config-file server).
- `registry.require_approved`: refuse prompts that are not hash-signed and approved.

## Guardrails and limits of the draft

- Prompts load only when their SHA-256 matches `prompts/manifest.yaml`; vet a changed prompt with
  `scr-agents registry sign <name> <path>`.
- The sandboxed shell confines cwd, gives each sandbox a throwaway `HOME`, scrubs the environment,
  enforces an allowlist plus per-binary argument policies (no `python -c`, `node -e`, `find -exec`,
  `git -c`, read-only git subcommands, absolute paths only under system prefixes), clamps timeouts,
  kills the whole process group and caps output. Build and test tools still run the reviewed
  project's own code by design, and the shell does **not** isolate the network or filesystem for
  them; run the whole system inside a VM or container for the "sovereign sandbox" guarantee.
- `.scr_agents/` in the reviewed repository can suppress findings and choose build commands, so it
  is read from the base commit whenever the change under review touches it, symlinks there are
  never followed, and `project.yaml` must validate against its schema or is ignored.
- `scr-agents review` exits 2 when an agent failed (the report is marked incomplete and lists why)
  and, with `--fail-on confirmed|unverified`, 1 when such findings exist.
- Running a project's tests needs that project's toolchain available on the host.
- The code graph is `ast`-accurate for Python and regex-based for C/C++/Go/Rust/JS/TS/Java and
  others.

## Deployment on the 6x RTX 6000 Ada server

**One-shot setup.** `python3 deploy/setup.py` asks a short series of questions (deployment style,
engine, models, GPU split, quantization, ports, scanners), each with a default you can accept with
Enter, then writes `deploy/generated/` (compose file or start script, `scr_agents.yaml`,
`download_models.sh`, `answers.json`) and offers to download the models, start the services, wait
for them and run `scr-agents llm check`. `--yes` takes every default, `--answers file` replays
saved answers on another host, `--dry-run` only generates. Tool-call and reasoning parsers are
suggested from the model name (Gemma 4, Llama 3, Qwen 3, DeepSeek, Mistral) and the script prints
a weight/KV-cache estimate per instance so a layout that does not fit is caught before start.

`deploy/` also holds a hand-written layout for one server with 6x RTX 6000 Ada (48 GB each), 2x EPYC 7653
and 512 GB RAM. The six GPUs are split between two independent models so the validator really is
a second model:

| GPUs | service | model | precision / parallelism | role |
|---|---|---|---|---|
| 0-1 | `vllm-reviewer` | `google/gemma-4-31B-it` | bf16, tensor parallel 2 | lead agent, planner, three subagents |
| 2-5 | `vllm-validator` | `meta-llama/Llama-3.3-70B-Instruct` | fp8 (on-the-fly), tensor parallel 4 | validator |
| CPU | `scr-agents` | - | 48 cores, 128 GB | console, chat, scanners, builds and tests |

Memory budget (0.92 of 48 GB usable per GPU): Gemma 4 31B weights take about 31 GB per GPU at
TP=2, leaving roughly 26 GB of KV cache across the pair; Llama 3.3 70B in fp8 takes about 18 GB per
GPU at TP=4, leaving about 100 GB of KV cache, enough for six concurrent validations at 64k
context. Drop `--quantization fp8` for exact bf16 weights (35 GB per GPU, KV cache shrinks to
about 36 GB). Both instances use `--max-model-len 65536`; the config caps diffs at 60k characters.
No NVLink on this card, so tensor parallel traffic goes over PCIe; that costs some throughput but
works fine at TP 2 and 4.

```bash
cd deploy
export HF_TOKEN=hf_...            # gated repos: accept both licenses on Hugging Face first
./download_models.sh              # one-time, online: weights + vLLM tool chat templates

docker compose up -d              # two vLLM services + the scr_agents sandbox container
docker compose exec scr_agents scr-agents llm check
open http://<server>:8765
docker compose exec -it scr_agents scr-agents chat https://github.com/org/repo.git
```

Bare metal instead of containers: `deploy/start_vllm.sh` starts the same two instances on
ports 8000 and 8001, and `--config deploy/scr_agents.baremetal.yaml` points scr_agents at them.

Isolation: the `scr-agents` container is the sovereign sandbox. It runs with all capabilities
dropped, CPU, memory and pid limits, and reaches the models over an internal Docker network with no
route outside. It also joins an `edge` network so the console is reachable and `git clone` works;
remove that network for a strictly offline box and reach the console through an SSH tunnel.
Reviewed code is built and tested inside this container only.

## Next steps (open questions from the spec)

- Target languages for the code graph beyond Python (tree-sitter would replace the regexes).
- Which scanners to standardize on; `semgrep --config auto` needs network, pin a ruleset offline.
- The QEMU/docker bench runners: board images, cross-compilation, how builds are cached.
- Where human verdicts and registry A/B statistics live long term (v0: JSONL on disk).
- A GitHub/GitLab adapter that posts the evidence report back to the PR.

## Development

The repository's CI runs the same checks on every pull request:

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run --group docs sphinx-build -W --keep-going -b html docs docs/_build/html
```

## Layout

```
src/scr_agents/
  models.py            shared input format and all downstream records
  config.py            settings (packaged defaults + YAML + env)
  pipeline.py          diagram-order wiring
  cli.py               scr-agents review | chat | serve | demo | verdict | mcp | skills | ...
  sessions.py          sessions and runs under out/sessions/
  inputs/              git, PR, project path or URL, request builder
  llm/                 backends (OpenAI-compatible for local servers and the OpenAI API, Anthropic, offline), tool loop, MCP client and registry
  ws1_review/          orchestrator, subagents, validator, tools
  ws2_analysis/        code graph, scanners
  ws3_harness/         workspace, shell, test bench, evidence
  ws4_guardrails/      safety filter, web search, registry
  ws5_knowledge/       knowledge base, activity log, human review, project memory, skills loader
  web/                 console: Starlette API, SSE, static single-page UI
  tui/                 terminal chat and live view
  prompts/             approved system prompts + manifest
  knowledge/           CWE / CERT C / MISRA seed entries
  skills/              shared_skills/ and one folder per agent
  configs/default.yaml
mcp_servers/                  MCP servers scr_agents can start, one folder and mcp.yaml each
deploy/                compose, Dockerfile, setup.py, vLLM scripts
tests/scr_agents/
docs/scr_agents/
```
