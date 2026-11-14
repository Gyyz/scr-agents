# scr_agents: command cheat sheet

All commands run from the repository root. `uv run` uses the project venv (`.venv`, Python 3.12).
Global options go before or after the subcommand: `--config FILE`, `--mode auto|local|offline|anthropic`,
`--orchestrator deterministic|openflow` (who runs a review round: code, or an LLM with tools), `--out DIR`
(default `out`), `-v`.

## 1. Install and verify

```bash
uv sync                                    # install everything (the default groups: full + dev)
uv run pytest -q                           # offline test suite, no GPU or network needed
uv run ruff check src tests deploy/setup.py
uv run scr-agents demo --mode offline       # plants defects in a temp repo and reviews it end to end
```

## 2. Local model server (vLLM or SGLang)

```bash
# Gemma 4 31B (default preference), vLLM >= 0.19
vllm serve google/gemma-4-31B-it --port 8000 --tensor-parallel-size 2 \
    --enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4 \
    --chat-template examples/tool_chat_template_gemma4.jinja

# Llama 3.3 70B as the second model (validator)
vllm serve meta-llama/Llama-3.3-70B-Instruct --port 8001 --tensor-parallel-size 4 --quantization fp8 \
    --enable-auto-tool-choice --tool-call-parser llama3_json \
    --chat-template examples/tool_chat_template_llama3.1_json.jinja

# SGLang alternative
python -m sglang.launch_server --model-path meta-llama/Llama-3.3-70B-Instruct --port 8001 --tool-call-parser llama3

# does the server do tool calls and JSON output? (uses llm.local.base_url, default http://127.0.0.1:8000/v1)
uv run scr-agents llm check
uv run scr-agents llm check --base-url http://127.0.0.1:8001/v1 --model meta-llama/Llama-3.3-70B-Instruct
```

Point scr_agents at the servers with a config file (copy `src/scr_agents/configs/default.yaml`, or use
`deploy/scr_agents.baremetal.yaml`), then pass `--config` to every command below.

## 3. Review

```bash
# a commit range in a local repo
uv run scr-agents review --repo /path/to/repo --base main --head HEAD

# a git URL or owner/repo (cloned under out/projects/), with a live terminal view
uv run scr-agents review --repo https://github.com/org/repo.git --base v1.2.0 --head v1.2.1 --live

# the whole repository at head, with focus instructions
uv run scr-agents review --repo . --base HEAD --head HEAD --scope all --instructions "memory safety in the parser"

# verify that a change fixes something
uv run scr-agents review --repo . --base abc123~1 --head abc123 --task verify_fix \
    --claim "fixes the heap overflow in parse_header" --pr gh:42

# CI gate: non-zero exit when confirmed findings exist
uv run scr-agents review --repo . --base origin/main --head HEAD --fail-on confirmed

# force a mode (auto = local server if reachable, else offline)
uv run scr-agents review --repo . --base main --mode local

# let an LLM orchestrate the round (reviewers and validator as its tools)
uv run scr-agents review --repo . --base main --orchestrator openflow
```

Results: `out/sessions/<session_id>/<run_id>/report.html` (the detailed report: one self-contained page to open or
send), `report.md`, `report.json`, `activity.jsonl`, `request.json`, `human_review.md`. In the console each finished
review has **Open report** and **Download HTML** (a copy without this machine's paths, for people without console access).

## 4. Interactive session (Claude Code-style)

```bash
uv run scr-agents chat /path/to/repo
uv run scr-agents chat https://github.com/org/repo.git
uv run scr-agents chat org/repo --scope all
uv run scr-agents chat /path/to/repo --ask "check the last 3 commits for injection bugs"   # one request, then exit
```

Inside the session: type requests in plain language, or
`/review [base..head|all]`, `/scope`, `/findings`, `/show <id>`, `/log [n]`, `/runs`,
`/verdict <id> accept|reject [note]`, `/memory`, `/remember <text>`, `/web`, `/verbose`, `/help`, `/quit`.

## 5. Web console

```bash
uv run scr-agents serve                                   # prints http://127.0.0.1:8765/?token=...
uv run scr-agents serve --host 0.0.0.0 --port 8765        # remote access: add the server name to web.allowed_hosts
SCR_AGENTS_TOKEN=mysecret uv run scr-agents serve          # stable token instead of one generated per start
```

Open the printed URL (the token becomes a cookie). API clients send `Authorization: Bearer <token>`:

```bash
curl -H "Authorization: Bearer $SCR_AGENTS_TOKEN" http://127.0.0.1:8765/api/runs
curl -H "Authorization: Bearer $SCR_AGENTS_TOKEN" -X POST http://127.0.0.1:8765/api/review \
     -H 'content-type: application/json' -d '{"repo":"https://github.com/org/repo.git","base":"main","head":"HEAD"}'
```

## 6. Results, feedback, memory

```bash
uv run scr-agents sessions                                 # sessions and their runs
uv run scr-agents sessions tidy                            # move pre-session out/run-* into out/sessions/legacy
uv run scr-agents verdict --run run-xxxxxxxx --finding F-xxxxxxxx --verdict accept --note "reproduced"
uv run scr-agents verdict --run run-xxxxxxxx --finding F-xxxxxxxx --verdict reject --note "intentional"
uv run scr-agents memory show --repo /path/to/repo         # <repo>/.scr_agents/: notes, runs, rejected findings
uv run scr-agents memory note --repo /path/to/repo --section "Build and test" "run make deps before pytest"
uv run scr-agents registry list                            # signed prompts
uv run scr-agents registry sign <name> src/scr_agents/prompts/<name>.md   # after editing a prompt
uv run scr-agents registry stats                           # human acceptance per prompt variant
uv run scr-agents kb strcpy buffer overflow                # knowledge base lookup
```

## 7. Server deployment (6x RTX 6000 Ada)

```bash
python3 deploy/setup.py                 # interactive: models, GPU split, ports...; generates deploy/generated/
python3 deploy/setup.py --yes           # all defaults
python3 deploy/setup.py --dry-run       # generate files only
python3 deploy/setup.py --answers deploy/generated/answers.json   # replay saved answers

# docker compose layout (after deploy/download_models.sh with HF_TOKEN set)
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml exec scr_agents scr-agents llm check
docker compose -f deploy/docker-compose.yml exec -it scr_agents scr-agents chat https://github.com/org/repo.git
docker compose -f deploy/docker-compose.yml logs -f scr_agents      # the console URL with its token is printed here

# bare metal
deploy/start_vllm.sh                    # two vLLM instances on :8000 and :8001
uv run scr-agents --config deploy/scr_agents.baremetal.yaml llm check
uv run scr-agents --config deploy/scr_agents.baremetal.yaml serve --host 0.0.0.0
deploy/start_vllm.sh stop
```
