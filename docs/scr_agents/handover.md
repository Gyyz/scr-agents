# scr_agents handover

Written for the people and AI agents who take over `scr_agents`: enough to have it running on a
server within half an hour and to change code within an hour. Paths such as `ws1_review/lead.py`
are relative to `src/scr_agents/`; run artefacts such as `report.md` and `activity.jsonl` live in
`out/sessions/<session_id>/<run_id>/`.

## 1. What it is

`scr_agents` is a self-hosted multi-agent code review system.
Given a git repository (a local path or a URL) and a scope (a commit range or the whole repository),
it produces an evidence-backed report of bugs, vulnerabilities and style problems. Every finding is
re-checked by a second model that can build the code, run its tests and run proofs of concept in a
sandbox, and ends up `confirmed`, `rejected` or `unverified`.

Other tools plug into it:

- **MCP servers** (`mcp_servers/`): web search today; knowledge base and emulator servers plug in
  the same way. `scr_agents` never imports a server's code; it starts the servers and calls their
  tools, so each part stays independently runnable.
- **Skills** (`src/scr_agents/skills/`): review guidance written by the operators, shared or per agent.

The design follows an architecture diagram with five workstreams (WS1 to WS5); the package keeps
that split (`ws1_review/` to `ws5_knowledge/`).

Hard constraints from the project lead:

- No online model API. Models are served locally by vLLM, SGLang or the company's GPUStack
  (OpenAI-compatible). The Anthropic backend exists only behind an explicit `llm.mode: anthropic` and
  is not installed by default.
- Default models: Gemma 4 31B (`google/gemma-4-31B-it`, `gemma4-31b-it` on GPUStack) for the
  reviewers and Llama 3.x 70B for the validator.
- Target server: 6x RTX 6000 Ada (48 GB), 2x EPYC 7653, 512 GB RAM.

## 2. Status

| Capability | State | Tests |
|---|---|---|
| Inputs: diff, commit messages, PR, path or URL, whole-repository scope | done | `test_git_inputs.py`, `test_project_and_session.py` |
| WS2 code graph (Python AST, regex for other languages), scanners (ruff, bandit, semgrep, cppcheck, gosec, built-in rules) | done; regex symbol extraction is approximate | `test_code_graph.py`, `test_scanners.py` |
| WS3 checkouts, sandboxed shell, test bench (local runner only), evidence report (Markdown, JSON, HTML) | done; docker and qemu runners are placeholders | `test_shell.py`, `test_pipeline_offline.py`, `test_report_html.py` |
| WS4 safety filter, signed prompt registry, allowlisted web search | done | `test_safety_filter.py`, `test_registry.py`, `test_web_search.py` |
| WS5 seed knowledge base, activity log, project memory, human verdicts, skills | done | `test_knowledge.py`, `test_project_memory.py`, `test_skills.py` |
| WS1 planner, three reviewer subagents, validator, chat lead agent | done | `test_pipeline_local.py` (a fake OpenAI server runs the whole pipeline) |
| WS1 OpenFlow orchestrator (`review.orchestrator: openflow`): an LLM runs the round with tools | done offline; not yet run against the GPU host's models | `test_openflow.py` |
| MCP: servers as folders in `mcp_servers/`, per-agent assignment, call logging | done | `test_mcp_registry.py`, `test_mcp_activity.py`, `test_harness_integration.py` |
| MCP replies in any format (JSON, CSV, TSV, Markdown, HTML, XML, text; images and resources) | done | `test_mcp_results.py` |
| Local model backend (vLLM, SGLang, GPUStack) | done; run against the GPU host's vLLM | `test_local_backend.py`, `scr-agents llm check` |
| Offline backend (rule-based, no model) | done; all tests depend on it | every test |
| Web console: chat, live progress, panels (orchestrator, MCP, memory and KV cache, activity), traces, configure menu, HTML report | done, checked in a browser | `test_web.py`, `test_web_chat.py`, `test_settings_api.py` |
| Terminal: `scr-agents chat` with a live view and slash commands | done | `test_project_and_session.py` |
| Deployment: compose, Dockerfile, bare-metal scripts, interactive `deploy/setup.py` | done; compose not yet brought up on the server | `test_deploy.py`, `test_setup_script.py` |

All tests run offline: no GPU, model server or network.

## 3. Getting it running

```bash
uv sync                                    # Python 3.12; installs the "full" group (the agents)
uv run pytest                              # the offline test suite
cp .env.example .env                       # optional: GPUSTACK_BASE_URL / GPUSTACK_API_KEY, MLflow

# models: GPUStack through .env, or your own vLLM/SGLang (interactive, every answer has a default)
python3 deploy/setup.py                    # or --yes; --dry-run only writes the files
#   writes deploy/generated/{docker-compose.yml | start_llm.sh, scr_agents.yaml, download_models.sh, answers.json}

uv run scr-agents --config deploy/generated/scr_agents.yaml llm check    # reviewer and validator servers: tool calls and JSON OK?
uv run scr-agents demo --mode offline                                     # end-to-end smoke run, no model
uv run scr-agents --config deploy/generated/scr_agents.yaml chat https://github.com/org/repo
uv run scr-agents --config deploy/generated/scr_agents.yaml serve        # prints http://<host>:8765/?token=...
```

With docker, the last commands become
`docker compose -f deploy/generated/docker-compose.yml exec scr-agents scr-agents llm check` and so on.
A local config with LAN addresses belongs in `scr_agents.yaml` at the repository root, which is
gitignored; `deploy/scr_agents.yaml` is the tracked template.

## 4. How a review runs (`pipeline.run_review`)

1. **Settings.** `mcp_registry.effective` adds the MCP servers switched on in `mcp_servers/`, and binds
   `web_search` if the console chose a server for it.
2. **Request.** `inputs.request.build_request` resolves base and head into a `ReviewRequest`: changed
   files and hunks, commit messages, PR, the user's words (`instructions`) and `scope`. With
   `scope="all"` every code file at head counts as added.
3. **Checkouts.** `Workspace` exports base and head with `git archive` into plain folders
   (`<run>/worktrees/`, deleted at the end). Without `.git`, code built and run there cannot reach the
   repository, its hooks or its config; symlinks pointing outside are skipped. Every host-side git call
   goes through `inputs/git.py` (`git()` and `git_cmd()`): hooks and fsmonitor off, only allowlisted
   environment variables, `diff --no-ext-diff`, refs after `--end-of-options`.
4. **WS2.** `CodeGraph.build(head)` then `blast_radius`; `run_scanners` keeps hits on changed lines.
5. **WS3.** `TestBench.run_both()` detects build and test commands and runs them on base and head.
   `ensure_tool_path()` first puts the interpreter's own `bin` folder on `PATH`, so the scanners
   and pytest installed next to `scr-agents` are found however it was started; a Python without
   the test runner (`No module named pytest`) gives a skipped bench, not failing tests.
6. **WS4.** `SafetyFilter` strips hidden characters, redacts secrets, flags injection attempts,
   truncates, and wraps the diff, commit messages, PR text and tool results in
   `<untrusted source=...>`.
7. **WS1 review.** `ReviewAgent.plan` (one model call: focus files and hypotheses; the planner's own
   skills are inline) then runs the three subagents in parallel. Each is a `ToolLoop` with read-only
   tools plus `report_finding` (findings arrive as validated tool calls). Then:
   - `merge_findings` merges reports on the same file within 3 lines, recording `merged_from` and
     `agreement`; the merged finding is a vulnerability when any of the reports is one;
   - `absorb_scanner_hits` lets a nearby scanner hit strengthen a finding, and promotes unreported
     critical or high hits;
   - the project memory's ignore rules apply;
   - `ground_findings` checks that the path and line exist (and, in range mode, are within 10 lines
     of a change) and fills `code_quote`;
   - `rank_key` orders the candidates.
   With `review.orchestrator: openflow` steps 7 and 8 run differently; see **Orchestrators** below.
8. **Validation.** `ValidatorAgent.validate` runs one `ToolLoop` per candidate on the second model,
   with `run_tests`, `write_file` (only into `.scr_agents_scratch/<finding>/`) and `run_command` on top
   of the read-only tools. Style and naming candidates get read-only tools, fewer turns and a cap per
   run. The validator sees only the claim, not the reviewer's confidence or rationale. Its own tool
   calls become evidence; `evidence_strength` records `differential` (base and head behave
   differently), `executed`, `traced` or `none`.
9. **Report.** `report.md`, `report.json`, `report.html` and `human_review.md` are written to the
   run's folder, with the round's two pages: `report_a.html` (the Review Report, each finding with
   the validator's comment) and `report_b.html` (the Validator Report, the validator's comment
   report on it); `activity.jsonl` is written throughout. A failing subagent or validator is recorded in
   `report.errors` (the report is marked incomplete and `review` exits 2) and never ends the run.

**Orchestrators** (`review.orchestrator`, `--orchestrator`, `SCR_AGENTS_ORCHESTRATOR`). Two modes
run steps 7 and 8:

- `deterministic` (the default): the fixed flow above. Code decides the order: planner, the three
  subagents in parallel, merge and rank, then the validator on the best candidates.
- `openflow` (`ws1_review/openflow.py`): one `ToolLoop` on the reviewer model, agent
  `orchestrator`, prompt `openflow_orchestrator`. It gets the same brief as the planner
  (`change_overview`) and, on top of the read-only tools (with its own skills folder and the MCP
  servers assigned to `orchestrator`), three tools: `run_reviewer` (one subagent, on the files,
  hypotheses and notes it picks; at most 9 runs per round), `list_candidates` and `validate`
  (the independent validator, on the ids it picks). It answers with the ids to report, the ones
  it drops (each with a reason) and notes, which become the report's plan notes.

  Code keeps the deterministic guardrails: new reports merge into existing candidates (whose ids
  stay valid), the project memory's ignore rules and settled findings of earlier rounds apply,
  every candidate is grounded, the validation budget (`llm.max_findings_to_validate`, the style
  limit) counts across the whole round, and verdicts are the validator's. At the end scanner hits
  are absorbed, a confirmed finding is always reported, a candidate the answer neither reports
  nor drops is reported anyway, and reported candidates nobody validated are validated while the
  budget lasts. Drops are listed in `stats.openflow.dropped` and the `openflow_dropped` event. If
  the orchestrator fails before any reviewer ran, the round runs the deterministic review instead
  (`openflow_fallback`) and the report is marked incomplete. A later round keeps the orchestrator
  of the round before. The console's welcome form has a Deterministic / OpenFlow switch next to
  **Start** (default `review.orchestrator`, then the browser's last choice): the chat keeps it in
  its session and runs every review with it (`orchestrator` in `POST /api/chats/new`). A single
  review can also name one: `orchestrator` in `POST /api/runs` and in the lead agent's
  `start_review`.

`ToolLoop` keeps the context in budget: when the prompt plus `max_tokens` would pass 90% of the
model's context window, old tool results are replaced by one-line stubs; on a context-length
error it compacts and retries once; the last turn offers no tools and asks for the final JSON.
The context window is the `max_model_len` the model's server reports in `GET /models` (read
once per run, separately for the reviewer and the validator), else `llm.local.context_window`.

**Review rounds** (`ws1_review/rounds.py`). Every run is one round. After it, a finding is
*settled* when the person reviewing agrees with the validator (confirmed and accepted, rejected
and rejected, unverified and accepted) and *pending* otherwise, a missing human verdict included.
When the validator did not confirm every finding, the lead agent shows the Validator Report and asks whether to
start the next round (`start_next_round`; `/next` in the terminal, **Review again** in the
console, `scr-agents next-round --run <id>` on the command line). The next round is a new run in
the same session (`run_next_round`): the same request with a new run id, `previous_run` pointing
at the round before. Its planner and subagents get the pending findings, the validator's comments
and the human verdicts as an `<untrusted source="previous_round">` section, the pending files go
first in the plan's focus, and they look at those findings and their surroundings again. New
candidates that repeat a settled finding are dropped, the settled findings are carried into the
report (`carried_from`, `human_verdict`), and the pending ones are replaced (`replaced`). The
reports are the Review Report and the Validator Report in round 1, then "Review Report (round 2)"
and so on (the files stay `report_a.html` and `report_b.html`, and the `label_a` / `label_b` fields
carry the names). `review.max_rounds` (default 3)
counts rounds in total. `--auto-rounds` (`review`, `demo`) has no one to ask: the validator's
confirmed and rejected verdicts settle findings and unverified ones are reviewed again until none
is left or the limit is reached. `round_status` (in `report_b.html`, `GET /api/runs/<run>/round`
and the lead agent's summary) is computed from `report.json` and the human verdicts recorded since.

**Sessions.** Every run belongs to a session (`sessions.py`): a `chat` conversation, one `serve`
process, or one `review` or `demo` invocation. `session.json` records its kind, creation time and
repository. `scr-agents sessions` lists them; `scr-agents sessions delete <id>` (or the console's
delete button) removes a conversation or a run.

**Chat.** `tui/session.py`'s `ChatSession` and the console's `WebChat` (`web/chat.py`) each hold a
`LeadAgent` (`ws1_review/lead.py`); its `start_review` tool is the whole pipeline above. Each answer
sees the last 6 messages of the conversation (600 characters each) and a summary of the last review.

## 5. Project memory (`<repo>/.scr_agents/`)

Every reviewed repository carries its own memory, shared with the team by committing it
(`ws5_knowledge/project_memory.py`):

| File | Content | Written by |
|---|---|---|
| `memory.md` | notes for the agents: Build and test, Conventions, Known false positives, Notes | people (editing, `scr-agents memory note`, `/remember` in chat) and the lead agent (`memory_note`) |
| `project.yaml` | overrides: `bench.build_cmd` and `test_cmd`, `ignore.paths` (globs), `ignore.rules` (scanner rules) | people |
| `findings.jsonl` | one line per finding per run with the agent's verdict, updated with human verdicts; findings are matched across runs by fingerprint | the pipeline, `record_verdict` |
| `state.json` | summaries of the last 20 runs | the pipeline |

A repository reviewed before the rename keeps using its `.codecheck/` folder until a `.scr_agents/`
one exists. The planner and subagents get `memory.md` and the findings people rejected; the validator
gets that finding's history; the ignore rules filter scanner hits and findings; every agent has
`memory_read`. All of it counts as repository content and is wrapped by the safety filter.

**Trust boundary.** The memory can suppress findings and choose build commands, so the change under
review must not be able to rewrite it: when the range touches the memory folder, it is read from the
base commit (`ProjectMemory(read_ref=base)`). Symlinks in or at the folder are never followed;
`project.yaml` is validated by `ProjectOverrides` and ignored entirely when invalid.

## 6. Configuration

Defaults: `src/scr_agents/configs/default.yaml`; override with `--config my.yaml` (deep merge).

| Key | Meaning |
|---|---|
| `llm.mode` | `auto` (local when the server answers, else offline), `local`, `offline`, `anthropic` (explicit opt-in) |
| `llm.local.base_url`, `model`, `validator_base_url`, `validator_model` | reviewer and validator servers and models; an empty `model` picks the first served model matching `preferred_models` |
| `llm.local.json_repair` | ask once more with `response_format` when a final answer is not JSON (never on normal turns: constrained decoding suppresses tool calls) |
| `llm.local.extra_body` | passed through to the server, e.g. `{chat_template_kwargs: {enable_thinking: false}}` |
| `llm.local.context_window` | used when the server does not report `max_model_len`; `ToolLoop` compacts near it |
| `llm.local.cache_metrics_interval_s` | how often the servers' `/metrics` prefix-cache counters are sampled during a run (0 = off) |
| `llm.parallel_subagents`, `parallel_validations`, `max_findings_to_validate`, `max_turns` | concurrency and limits |
| `scanners.enabled`, `semgrep_config` | offline semgrep needs a local rules folder (`/opt/semgrep-rules` in the image) |
| `shell.allowed_binaries`, `denied_args`, `allowed_subcommands`, `python_modules`, `allowed_path_prefixes`, `env_passthrough` | the sandbox's allowlist and per-binary argument policy; HOME is always a throwaway folder |
| `mcp_servers_dir` | the folder of MCP servers (default `mcp_servers/`) |
| `mcp_servers` | extra stdio servers defined in the config file, always on, optionally limited with `components` |
| `web_search.allowlist` | domains whose results reach the agents, whichever server backs web search |
| `skills.paths` | skill roots: `builtin` is `src/scr_agents/skills`; add folders with the same layout |
| `web.token`, `allowed_hosts`, `allow_local_paths`, `url_schemes` | console token (empty = generated and printed at start), Host allowlist, local paths, clone URL schemes |
| `review.max_rounds` | review rounds in total (default 3), see Review rounds above |
| `review.orchestrator`, `openflow_max_turns` | `deterministic` (default) or `openflow`, see Orchestrators above; the OpenFlow orchestrator's tool turns (default 30) |
| `bench.runner`, `build_cmd`, `test_cmd` | only `local` is implemented; empty commands are auto-detected |
| `output_dir`, `projects_dir` | run output; where URLs are cloned |

Environment: `SCR_AGENTS_MODE` overrides `llm.mode`, `SCR_AGENTS_TOKEN` the console token,
`SCR_AGENTS_ORCHESTRATOR` `review.orchestrator`, `SCR_AGENTS_MCP_SERVERS_DIR` the MCP folder. `GPUSTACK_BASE_URL` and `GPUSTACK_API_KEY` become the model
server unless a config file names one. `scr-agents` reads `.env` from the working directory.

## 7. Activity log events (the UI's data source)

Each record is `{seq, ts, run_id, commit_ids, event, agent, ...payload}`, appended to `activity.jsonl`
and pushed to subscribers (the console's SSE stream, the terminal renderer). **Add fields; never
rename existing ones.**

| Event | Agent | Main fields |
|---|---|---|
| `request` | | files, commits, task, base, head, paths, repo, claim |
| `stage` | | stage (inputs, graph, scanners, bench, review, validation, report), status (running, done), the stage's numbers; review running carries orchestrator |
| `mode` | | mode, reviewer_model, validator_model, base_url, independent_validator |
| `code_graph`, `scanners`, `test_bench` | | summary, changed_symbols; run, missing, findings, items; delta, base, head |
| `plan` | review_agent | plan: focus_files, hypotheses, notes |
| `subagent_start`, `subagent_done` | subagent:bugs and so on | model; findings, turns, error, usage |
| `llm_start`, `llm_call` | any agent | turn, model; stop_reason, usage, tool_uses, tool_inputs, text_preview, duration_s, trace. `usage.cached_input_tokens` appears when the server reports prefix-cache hits (vLLM `--enable-prompt-tokens-details`, SGLang `--enable-cache-report`) |
| `tool_start`, `tool_call` | any agent | tool, input, call_id; is_error, duration_s, output_chars, output_preview, trace; MCP tools add origin (`mcp:<server>`) and remote_name |
| `mcp_servers` | | servers: name, command (executable name only), status, error, connect_s, untrusted, components, tools (name, description, agent_tool); web_search |
| `mcp_start`, `mcp_call` | the calling agent | server, tool, input, call_id (the same as the tool call's); is_error, error, duration_s, output_chars, output_preview (the server's own reply, before filtering, secrets redacted), format (json, csv, tsv, markdown, html, xml, text, or mixed), parts (one per content item: type, format, mime, chars), trace |
| `memory_used` | review_agent, subagent:x, validator:F-x, lead | what an agent was given: kind=project (notes_chars, rejected, read_from; validators add finding, title and prior, the earlier reviews of the same finding) or kind=conversation (messages_total, messages_used, chars_cap, cut, used, last_review) |
| `kv_cache` | | servers: server, model, roles, run_queries, run_hits, run_hit_rate, usage_perc, lifetime figures, error; final. Server-wide counters from `/metrics` |
| `candidates` | review_agent | count, ids, raw, grounded, agreed, duplicates, promoted, ignored |
| `openflow_start`, `openflow_done` | orchestrator | model; turns, reviewer_runs, candidates, validated, reported, dropped, promoted, left_out |
| `openflow_dropped` | orchestrator | findings: id, title, reason (left out of the report by the orchestrator) |
| `openflow_fallback`, `openflow_error` | orchestrator | step (review: the deterministic review ran instead); error, traceback |
| `validate_start`, `verdict` | validator, validator:F-x | finding, model; verdict, turns, error, prior, evidence_strength |
| `context_compacted` | any agent | turn, freed_chars, reason |
| `report` | | counts, markdown, json, packet, incomplete, errors, round, report_a, report_b |
| `round` | | round, max_rounds, previous_run, carried, pending, auto |
| `round_repeats` | review_agent | count, ids: new candidates dropped because they repeat a settled finding |
| `round_status` | | round, max_rounds, label_a, label_b, auto, validator_passed, settled_real, settled_false, pending, disputed, awaiting_verdict, passed, can_continue, ask_user, report_a, report_b (before any human verdict) |
| `skills` | | count, names, paths, errors, disabled, components |
| `memory`, `memory_ignored`, `memory_recorded`, `memory_error` | | exists, notes_chars, history, overrides, read_from; count, stage, ids; findings, dir; error |
| `llm_error`, `run_error`, `mcp_error`, `plan_failed`, `finding_parse_error`, `evidence_parse_error` | | error |
| `subagent_error`, `validator_error` | subagent:x, validator:F-x | error, traceback (the agent itself crashed) |
| `run_finished`, `run_failed` | | made up by `web/runs.py`, not in the file; run_finished carries errors |
| `message` | chat | role, text, intro, error |
| `chat_status` | chat | busy; phase=preparing while the repository is fetched |
| `repo_preparing`, `repo_ready`, `repo_failed` | chat | target, action (clone, update, open); repo, files, head, branch, duration_s; error |
| `run_started`, `run_done` | chat | run, base, head, scope, task, files, round and previous_run (a later round); status, counts, error |
| `replay_done` | | sent by `/api/chats/<id>/events` after the replay: live, busy |

**Traces.** Every model call, tool call and MCP call also writes one JSON file to the run's `trace/`
folder; the event's `trace` field is its relative path. A model call's trace holds the full request
(system, messages, tools, output schema), the normalised response, and `exchanges`: the backend's
actual HTTP requests and responses, retries and JSON repairs included. Request headers are not
recorded, so API keys never reach a trace. The console serves them from
`/api/runs/<run>/trace/<file>` and `/api/chats/<chat>/trace/<file>`.

**The console.** `web/static/app.js` derives its steps from these events: keys `<run_id>|<key>` with
`agent:*`, `llm:<agent>:<turn>`, `tool:<agent>:<call_id>` and `plan`; MCP calls attach to their tool
step as `result.mcp`. Side panels: Orchestrator (`renderOrchestrator`), MCP (`renderMcp`), Memory
(`renderMem`: conversation, project memory hits, KV cache) and Activity. The top-left **Configure**
menu (`renderSettings`) edits `mcp_servers/config.yaml` and `src/scr_agents/skills/config.yaml` through
`/api/settings/...`. Each finished review offers **Open report** and **Download HTML**
(`/api/runs/<run>/report.html`; `?download=1` leaves out this machine's paths).

## 8. Extension points

- **A skill:** a `SKILL.md` in `src/scr_agents/skills/shared_skills/<name>/` (every agent) or in one
  agent's folder (`planner`, `bugs`, `vulnerabilities`, `style`, `validator`, `lead`). No code and no
  signing needed. See {doc}`extending`.
- **An MCP server:** a folder in `mcp_servers/` with an `mcp.yaml` (command, args with `{dir}`, `{root}` and
  `{python}` placeholders, `env_from_host`, `requires`, an optional `web_search` binding). New folders
  start off; first-party ones can set `enabled_by_default`. The console switches servers and assigns
  agents but cannot change a command. See {doc}`extending`.
- **An agent tool:** a `ToolSpec` in `review_tools` or `validator_tools` (`ws1_review/tools.py`).
  Output that comes from the repository or from outside is `untrusted=True`; tools that write to the
  shared checkouts are `parallel_safe=False`. `test_agent_tools.py` snapshots each agent's tool set.
- **A scanner:** an entry in `SCANNERS` (`ws2_analysis/scanners.py`); built-in regex rules are in
  `BUILTIN_RULES`.
- **A subagent:** a `SubagentSpec` in `SUBAGENTS` (`ws1_review/subagents.py`), a prompt in `prompts/`
  signed with `scr-agents registry sign`, and an offline policy in `OfflineBackend`.
- **A model backend:** implement `LLMBackend` (`llm/backend.py`) and wire it into
  `pipeline.make_backend_factory`.

## 9. Known limitations

- Gemma 4's tool-call parser needs vLLM 0.19 or newer; run `scr-agents llm check` on every new server.
- Both default models are gated on Hugging Face: accept the licences and set `HF_TOKEN` to download,
  then run with `HF_HUB_OFFLINE=1`.
- The sandbox limits the working directory, HOME, the environment, binaries and their arguments,
  time and output. It does **not** isolate the network or the file system from build tools, which run
  the reviewed project's own code; real isolation comes from running `scr-agents` in its container
  (the compose service drops all capabilities and sits on an internal network).
- MCP servers run as normal processes with the user and network access of `scr-agents`: only add
  servers whose code you trust.
- The code graph is regex-based outside Python; tree-sitter is the natural next step.
- Large diffs are not split yet: with `scope=all` the diff is cut by characters and the planner does
  not see the files past the cut.
- Share links for HTML reports are not implemented (a page outside the console token needs a
  decision); use **Download HTML**.

## 10. Why it is built this way

- **A hand-written tool loop** rather than an SDK runner: one loop serves the local, offline and
  Anthropic backends and puts the safety filter and the logging on every tool result.
- **The offline backend is first-class:** the whole pipeline runs without a model, so tests and CI
  need no GPU.
- **A second model validates:** the architecture asks for an independent re-check; on the 6-GPU
  server each model has its own GPUs.
- **No `response_format` on normal turns:** vLLM's constrained decoding suppresses tool calls.
- **Signed prompts:** the approved registry refuses prompts that were not vetted.
- **Other tools through MCP:** `scr_agents` starts knowledge base and emulator servers instead of
  importing them, so their dependencies (and an emulator's Docker image) stay separate.
- **The console switches MCP servers but does not define them:** a command can only come from a file
  on disk, so the web API cannot make the host run anything new.
- **A dependency-free web front end:** no CDN and no build chain in an offline environment.
- **The UI is derived from events:** a past run replays from its `activity.jsonl` into the same view.

## 11. Next steps

1. Export the activity log as MLflow traces (the project's tracing standard).
2. A CWE knowledge base as an MCP server, and its data in reports: CWE names, and a flag on CWEs
   MITRE marks Discouraged or Prohibited for mapping.
3. An emulator as an MCP server: an `mcp_servers/scr-emulator/` manifest for the validator only, and
   its proof-of-concept skill in `src/scr_agents/skills/validator/`.
4. `bench.runner: docker` (one container per run), then QEMU for firmware builds.
5. Tree-sitter for the code graph; a pinned offline semgrep rule set.
6. Post reports to GitHub and GitLab pull requests as review comments.
