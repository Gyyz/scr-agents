"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from scr_agents.config import Settings


def _settings(args: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = {}
    if getattr(args, "mode", None):
        overrides.setdefault("llm", {})["mode"] = args.mode
    if getattr(args, "out", None):
        overrides["output_dir"] = args.out
    if getattr(args, "orchestrator", None):
        overrides.setdefault("review", {})["orchestrator"] = args.orchestrator
    return Settings.load(getattr(args, "config", None), overrides)


def _print_rounds(report: Any, run_dir: Path) -> None:
    """Print the round a run reached and the paths of its two round reports."""
    from scr_agents.ws1_review.rounds import run_status

    st = run_status(run_dir)
    if st.passed:
        state = "passed"
    elif not st.can_continue:
        state = f"{len(st.pending)} finding(s) pending; round limit reached"
    else:
        state = (
            f"{len(st.pending)} finding(s) pending; record verdicts, then "
            f"scr-agents next-round --run {report.run_id}"
        )
    print(f"round {st.round} of {st.max_rounds}: {state}")
    print(f"{st.label_a}: {run_dir / 'report_a.html'}")
    print(f"{st.label_b}: {run_dir / 'report_b.html'}")


def cmd_review(args: argparse.Namespace) -> int:
    """Handle ``scr-agents review``: run one review and print where the report is.

    Prints the run id, the verdict counts and the paths of ``report.md`` and
    ``report.html``; ``--live`` renders the agents' activity while the review runs.
    With ``--auto-rounds`` the findings the validator could not verify are reviewed
    again, round after round, up to ``review.max_rounds`` rounds.
    Errors of agents that failed go to stderr.

    Returns:
        2 if the review is incomplete (an agent failed), 1 if ``--fail-on`` is
        met, else 0.
    """
    from scr_agents.inputs.project import resolve_project
    from scr_agents.inputs.request import build_request
    from scr_agents.pipeline import run_auto_rounds, run_review
    from scr_agents.sessions import new_session

    settings = _settings(args)
    auto = bool(getattr(args, "auto_rounds", False))
    repo = resolve_project(args.repo, Path(settings.projects_dir))
    request = build_request(
        repo,
        args.base,
        args.head,
        pr=args.pr,
        task=args.task,
        claim=args.claim,
        instructions=args.instructions,
        scope=args.scope,
        max_files=settings.whole_repo_max_files,
    )
    request.session_id = new_session(settings, "cli", repo=str(repo), command="review")
    if args.live:
        from scr_agents.tui.render import LiveRenderer

        logging.getLogger("scr_agents").setLevel(logging.WARNING)
        renderer = LiveRenderer()
        with renderer:
            report, run_dir = run_review(
                settings,
                repo,
                args.base,
                args.head,
                request=request,
                on_event=renderer.handle,
                auto_rounds=auto,
            )
            if auto:
                report, run_dir = run_auto_rounds(
                    settings, report, run_dir, renderer.handle
                )
        renderer.print_summary(report, run_dir)
    else:
        report, run_dir = run_review(
            settings, repo, args.base, args.head, request=request, auto_rounds=auto
        )
        if auto:
            report, run_dir = run_auto_rounds(settings, report, run_dir)
    counts = report.counts()
    print(
        f"run {report.run_id}: {counts['confirmed']} confirmed, "
        f"{counts['unverified']} unverified, "
        f"{counts['rejected']} rejected -> {run_dir / 'report.md'}"
    )
    print(
        "detailed report (one HTML file you can open or send): "
        f"{run_dir / 'report.html'}"
    )
    _print_rounds(report, run_dir)
    for err in report.errors:
        print(f"  incomplete: {err}", file=sys.stderr)
    if report.incomplete:
        return 2
    if args.fail_on == "confirmed" and counts["confirmed"]:
        return 1
    if args.fail_on == "unverified" and (counts["confirmed"] or counts["unverified"]):
        return 1
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    """Handle ``scr-agents chat``: an interactive session on a project path or URL.

    With ``--ask`` it handles that one request and exits instead of prompting.

    Returns:
        Always 0.
    """
    from scr_agents.tui.session import ChatSession

    logging.getLogger("scr_agents").setLevel(
        logging.WARNING
    )  # the live view replaces the log lines
    settings = _settings(args)
    session = ChatSession(
        settings, args.target, base=args.base, head=args.head, scope=args.scope
    )
    if args.ask:
        session.banner()
        session.handle(" ".join(args.ask))
        session.log.close()
        return 0
    session.loop()
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Handle ``scr-agents demo``: build the planted-defect repository and review it.

    A non-empty ``--dir`` is replaced only if an earlier demo created it. Prints the
    repository location, the verdict counts and the report paths.

    Returns:
        1 if ``--dir`` holds something the demo did not create, else 0.
    """
    from scr_agents.demo import DEMO_MARKER, make_demo_repo
    from scr_agents.pipeline import run_auto_rounds, run_review

    settings = _settings(args)
    dest = (
        Path(args.dir)
        if args.dir
        else Path(tempfile.mkdtemp(prefix="scr-agents-demo-"))
    )
    if dest.exists() and any(dest.iterdir()):
        if not (dest / DEMO_MARKER).exists():
            print(
                f"{dest} is not empty and was not created by `scr-agents demo`; "
                "refusing to delete it",
                file=sys.stderr,
            )
            return 1
        shutil.rmtree(dest)
    from scr_agents.sessions import new_session

    base, head = make_demo_repo(dest)
    print(f"demo repo at {dest} ({base[:10]}..{head[:10]})")
    report, run_dir = run_review(
        settings,
        dest,
        base,
        head,
        session_id=new_session(settings, "demo", repo=str(dest)),
        auto_rounds=bool(getattr(args, "auto_rounds", False)),
    )
    if getattr(args, "auto_rounds", False):
        report, run_dir = run_auto_rounds(settings, report, run_dir)
    counts = report.counts()
    print(
        f"run {report.run_id}: {counts['confirmed']} confirmed, "
        f"{counts['unverified']} unverified, "
        f"{counts['rejected']} rejected -> {run_dir / 'report.md'}"
    )
    print(
        "detailed report (one HTML file you can open or send): "
        f"{run_dir / 'report.html'}"
    )
    _print_rounds(report, run_dir)
    return 0


def cmd_next_round(args: argparse.Namespace) -> int:
    """Handle ``scr-agents next-round``: review a run's pending findings again.

    Record the human verdicts first (``scr-agents verdict``): findings the person
    reviewing and the validator agree on are carried, the others are reviewed again
    from the planner with that round's Review Report and Validator Report as context.

    Returns:
        1 if the run is not found or cannot have another round, 2 if the new round
        is incomplete, else 0.
    """
    from scr_agents.pipeline import run_next_round
    from scr_agents.sessions import find_run_dir
    from scr_agents.ws1_review.rounds import RoundError

    settings = _settings(args)
    run_dir = (
        Path(args.run_dir) if args.run_dir else find_run_dir(settings, args.run or "")
    )
    if run_dir is None or not (run_dir / "report.json").is_file():
        print(f"run {args.run or args.run_dir!r} has no report", file=sys.stderr)
        return 1
    try:
        report, new_dir = run_next_round(settings, run_dir)
    except RoundError as e:
        print(str(e), file=sys.stderr)
        return 1
    counts = report.counts()
    print(
        f"run {report.run_id}: {counts['confirmed']} confirmed, "
        f"{counts['unverified']} unverified, "
        f"{counts['rejected']} rejected -> {new_dir / 'report.html'}"
    )
    _print_rounds(report, new_dir)
    for err in report.errors:
        print(f"  incomplete: {err}", file=sys.stderr)
    return 2 if report.incomplete else 0


def cmd_verdict(args: argparse.Namespace) -> int:
    """Handle ``scr-agents verdict``: record a human verdict on one finding.

    The run is ``--run-dir``, or ``--run`` looked up in the sessions. Prints the
    recorded verdict next to the agent's.

    Returns:
        1 if the run is not found, else 0.
    """
    from scr_agents.sessions import find_run_dir
    from scr_agents.ws4_guardrails.registry import PromptRegistry
    from scr_agents.ws5_knowledge.human_review import record_verdict

    settings = _settings(args)
    if args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        found = find_run_dir(settings, args.run or "")
        if found is None:
            print(
                f"run {args.run!r} not found under "
                f"{Path(settings.output_dir).resolve()}/sessions",
                file=sys.stderr,
            )
            return 1
        run_dir = found
    registry = PromptRegistry(
        settings.manifest_path(),
        settings.registry.require_approved,
        stats_path=Path(settings.output_dir).resolve() / "registry_stats.jsonl",
    )
    rec = record_verdict(run_dir, args.finding, args.verdict, args.note or "", registry)
    print(
        f"recorded {rec['verdict']} for {rec['finding_id']} "
        f"(agent said {rec['agent_verdict']})"
    )
    return 0


def cmd_registry(args: argparse.Namespace) -> int:
    """Handle ``scr-agents registry``: list or sign prompts, or show verdict stats.

    ``list`` prints every manifest entry with its approval state and hash;
    ``sign`` records a prompt file's hash, approved unless ``--pending``;
    ``stats`` prints the human-verdict counts per prompt variant.

    Returns:
        0, or 1 for an unknown registry subcommand.
    """
    from scr_agents.ws4_guardrails.registry import PromptRegistry

    settings = _settings(args)
    registry = PromptRegistry(
        settings.manifest_path(),
        require_approved=False,
        stats_path=Path(settings.output_dir) / "registry_stats.jsonl",
    )
    if args.registry_cmd == "list":
        for e in registry.entries():
            print(
                f"{e.name:28s} v{e.version:<3s} {e.variant}  "
                f"{'approved' if e.approved else 'PENDING ':9s} "
                f"{e.sha256[:12]}  {e.path}"
            )
        return 0
    if args.registry_cmd == "sign":
        e = registry.sign(
            args.name,
            Path(args.path),
            approve=not args.pending,
            version=args.version,
            variant=args.variant,
            description=args.description or "",
        )
        print(
            f"signed {e.name} -> {e.sha256[:12]} "
            f"({'approved' if e.approved else 'pending'})"
        )
        return 0
    if args.registry_cmd == "stats":
        for k, v in sorted(registry.stats().items()):
            print(f"{k:32s} accepted={v['accepted']} rejected={v['rejected']}")
        return 0
    return 1


def cmd_serve(args: argparse.Namespace) -> int:
    """Handle ``scr-agents serve``: run the web console until it is stopped.

    Returns:
        1 if the ``web`` extra is not installed, else 0 once the server exits.
    """
    try:
        from scr_agents.web.server import serve
    except ImportError as e:  # starlette / uvicorn missing
        print(
            f"web UI needs the 'web' extra: uv sync --extra web ({e})", file=sys.stderr
        )
        return 1
    settings = _settings(args)
    serve(
        settings, host=args.host, port=args.port
    )  # prints the URL with the access token
    return 0


def _check_llm_server(
    settings: Settings, role: str, base_url: str, model: str, hosted: bool = False
) -> bool:
    """Check one model server for ``scr-agents llm check`` and print the results.

    Checks reachability, the served models, the context window, tool calling and
    JSON output, one printed line each, with a hint on the server flags when no tool
    call is parsed.

    Args:
        settings: Loaded settings; ``llm.local`` gives the key, timeouts and limits
            (``llm.openai`` and its environment variable when ``hosted``).
        role: ``reviewer`` or ``validator``, printed as the heading.
        base_url: The server's OpenAI-compatible endpoint.
        model: The model to test; empty picks the first one the server lists.
        hosted: Check the hosted API of ``llm.mode: openai`` instead of a local
            server.

    Returns:
        Whether every check passed.
    """
    import json

    from scr_agents.llm.backend import BackendError
    from scr_agents.llm.local import LocalBackend

    cfg = settings.llm.openai if hosted else settings.llm.local
    block = "llm.openai" if hosted else "llm.local"
    api_key = (
        os.environ.get(settings.llm.openai.api_key_env, "")
        if hosted
        else settings.llm.local.api_key
    )
    print(f"== {role}")
    print(f"server   {base_url}")
    if hosted and not api_key:
        print(f"status   NO KEY ({settings.llm.openai.api_key_env} is not set)")
        return False
    if not LocalBackend.reachable(base_url, api_key, timeout=5):
        print(
            "status   UNREACHABLE "
            + (
                "(is the key valid and the base URL right?)"
                if hosted
                else "(is vLLM / SGLang running with an OpenAI-compatible endpoint?)"
            )
        )
        return False
    models = LocalBackend.list_models(base_url, api_key)
    model = model or (models[0] if models else "")
    if hosted:
        print(f"models   {len(models)} listed")
    else:
        print(f"models   {', '.join(models) or '(none listed)'}")
    print(f"using    {model}")
    window = LocalBackend.served_context_window(base_url, model, api_key)
    limit = window or cfg.context_window
    print(
        f"window   {limit} tokens "
        f"({'max_model_len' if window else f'{block}.context_window'})"
    )
    if cfg.max_tokens * 4 > limit:
        print(
            f"         -> {block}.max_tokens ({cfg.max_tokens}) takes more than a "
            "quarter of it: tool results are compacted early; lower max_tokens or "
            "serve a longer context"
        )
    backend = LocalBackend(
        base_url,
        model,
        api_key=api_key,
        temperature=settings.llm.openai.temperature if hosted else 0,
        max_tokens=512,
        timeout_s=cfg.timeout_s,
        json_repair=cfg.json_repair,
        extra_body=cfg.extra_body,
        token_param=settings.llm.openai.token_param if hosted else "max_tokens",
        name="openai" if hosted else "local",
    )
    tool = {
        "name": "lookup_cwe",
        "description": "Look up a CWE entry by id.",
        "input_schema": {
            "type": "object",
            "properties": {"cwe_id": {"type": "string"}},
            "required": ["cwe_id"],
        },
    }
    ok = True
    try:
        r = backend.complete(
            system=(
                "You are a code reviewer. "
                "Use the lookup_cwe tool when asked about a CWE."
            ),
            messages=[
                {
                    "role": "user",
                    "content": "What is CWE-78? Call the lookup_cwe tool first.",
                }
            ],
            tools=[tool],
            max_tokens=512,
        )
        calls = r.tool_uses()
        if calls and calls[0]["name"] == "lookup_cwe" and "cwe_id" in calls[0]["input"]:
            print(
                "tools    OK  (model called lookup_cwe with "
                f"{json.dumps(calls[0]['input'])})"
            )
        else:
            ok = False
            print(
                "tools    FAIL  (no parsed tool call; "
                f"stop_reason={r.stop_reason}, text={r.text()[:120]!r})"
            )
            print(
                "         -> start the server with a tool-call parser for your model, "
                "e.g. vLLM: --enable-auto-tool-choice --tool-call-parser hermes ; "
                "SGLang: --tool-call-parser qwen25"
            )
        schema = {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["answer", "confidence"],
            "additionalProperties": False,
        }
        r2 = backend.complete(
            system="Answer as JSON matching the schema.",
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Is strcpy safe? Reply as JSON with answer and confidence."
                    ),
                }
            ],
            tools=[],
            output_schema=schema,
            max_tokens=256,
        )
        from scr_agents.llm.loop import parse_json_object

        data = parse_json_object(
            r2.text()
        )  # the same lenient parser the agents use (fences, prose around JSON)
        if data is not None:
            fenced = not r2.text().strip().startswith("{")
            print(
                f"json     OK  ({json.dumps(data)[:100]}"
                f"{' · inside a code fence, accepted' if fenced else ''})"
            )
        else:
            ok = False
            print(f"json     FAIL  (final answer not JSON: {r2.text()[:120]!r})")
        print(f"usage    {r.usage} + {r2.usage}")
    except BackendError as e:
        ok = False
        print(f"error    {e}")

    return ok


def _check_hosted(settings: Settings, args: argparse.Namespace) -> int:
    """Handle ``scr-agents llm check`` for ``llm.mode: openai``.

    Checks the reviewer model and, when a different validator model is set, that
    one too, on the hosted API. Each check sends two short requests, which the
    provider bills.

    Returns:
        0 if every check passed, else 1.
    """
    cfg = settings.llm.openai
    base_url = args.base_url or cfg.base_url
    model = args.model or cfg.model
    if not model:
        print("llm.openai.model is empty: set it to the provider's model id")
        return 1
    ok = _check_llm_server(settings, "reviewer", base_url, model, hosted=True)
    vmodel = cfg.validator_model or model
    if not (args.base_url or args.model) and vmodel != model:
        ok = _check_llm_server(settings, "validator", base_url, vmodel, True) and ok
    print(
        "result   "
        + (
            "ready: run `scr-agents review --mode openai ...`"
            if ok
            else "fix the issues above"
        )
    )
    return 0 if ok else 1


def cmd_llm(args: argparse.Namespace) -> int:
    """Handle ``scr-agents llm check`` against the OpenAI-compatible servers.

    Checks the reviewer's server and, when the validator has its own server or
    model, that one too (see ``_check_llm_server``). ``--base-url`` or ``--model``
    checks only the server and model named. With ``llm.mode: openai`` the hosted
    API is checked instead of the local servers.

    Returns:
        0 if every check passed, else 1.
    """
    from scr_agents.pipeline import independent_validator

    settings = _settings(args)
    if settings.llm.mode == "openai":
        return _check_hosted(settings, args)
    cfg = settings.llm.local
    base_url = args.base_url or cfg.base_url
    model = args.model or cfg.model
    ok = _check_llm_server(settings, "reviewer", base_url, model)
    vurl, vmodel = cfg.validator_base_url or cfg.base_url, cfg.validator_model or model
    if args.base_url or args.model:
        pass  # an explicit target: check only that one
    elif independent_validator(model, vmodel, base_url, vurl):
        ok = _check_llm_server(settings, "validator", vurl, vmodel) and ok
    else:
        print(
            "== validator\n"
            "same model on the same server as the reviewer: findings are not "
            "re-checked independently (set llm.local.validator_model and "
            "validator_base_url)"
        )
    print(
        "result   "
        + (
            "ready: run `scr-agents review --mode local ...`"
            if ok
            else "fix the issues above"
        )
    )
    return 0 if ok else 1


def cmd_mcp(args: argparse.Namespace) -> int:
    """Handle ``scr-agents mcp check``: start the switched-on MCP servers and test them.

    Start the configured MCP servers, list their tools, and probe web search through
    the allowlist; with ``--query``, run that search and print the filtered result.

    Returns:
        0 if every server started and web search, when enabled, is connected; 1
        otherwise, including when no server is on or the ``mcp`` package is
        missing.
    """
    from scr_agents.llm.mcp_client import MCPClient
    from scr_agents.llm.mcp_registry import registry
    from scr_agents.ws4_guardrails.safety_filter import SafetyFilter
    from scr_agents.ws4_guardrails.web_search import WebSearch

    base = _settings(args)
    reg = registry(base)
    entries = reg.entries()
    for e in entries:  # every folder in mcp_servers/, on or off
        state = ("on " if e.enabled else "off") + (
            "" if e.ready else "  NOT READY: " + "; ".join(e.missing)
        )
        print(
            f"{e.manifest.name:14s} {state}   mcp_servers/{e.folder}  "
            f"{e.manifest.description[:70]}"
        )
        if not e.ready and e.manifest.install:
            print(f"{'':14s}       install: {e.manifest.install}")
    for err in reg.errors:
        print(f"mcp_servers    ERROR {err}")
    settings = reg.apply(base)
    if not settings.mcp_servers:
        print(
            "no MCP server is on: switch one on in the console "
            "(Configure -> MCP servers) or in "
            f"{reg.root / 'config.yaml'}; see mcp_servers/README.md"
        )
        return 1
    if not MCPClient.available():
        print("the `mcp` package is missing: uv sync --extra mcp")
        return 1
    client = MCPClient(settings.mcp_servers).start()
    ok = True
    try:
        for cfg in settings.mcp_servers:
            if cfg.name in client.errors:
                print(
                    f"{cfg.name:14s} FAILED  {cfg.command} {' '.join(cfg.args)}\n"
                    f"{'':14s}         {client.errors[cfg.name]}"
                )
                ok = False
                continue
            tools = client.toolspecs(cfg.name)
            print(
                f"{cfg.name:14s} OK      {len(tools)} tool(s) via "
                f"{cfg.command} {' '.join(cfg.args)}"
            )
            for t in tools:
                print(
                    f"    {t.name:40s} {(t.description.splitlines() or [''])[0][:88]}"
                )
        ws = settings.web_search
        if not ws.enabled:
            print("web_search     off     (web_search.enabled: false)")
        else:
            search = WebSearch(ws, client, SafetyFilter(settings.safety))
            if not search.available():
                print(
                    f"web_search     FAILED  web_search.mcp_server {ws.mcp_server!r} "
                    "is not a connected server"
                )
                ok = False
            else:
                print(
                    f"web_search     OK      {ws.mcp_server}.{ws.tool_name}"
                    f"({ws.query_arg}, {ws.limit_arg}); "
                    "allowlist: "
                    f"{', '.join(ws.allowlist) or '(empty: every result is dropped)'}"
                )
                if args.query:
                    print(f"probe {args.query!r}:")
                    print(search.search(args.query, 3))
    finally:
        client.close()
    for name, err in client.errors.items():
        if name.startswith("_"):
            print(f"shutdown: {err}")
    print("result   " + ("ready" if ok else "fix the failures above"))
    return 0 if ok else 1


def cmd_sessions(args: argparse.Namespace) -> int:
    """Handle ``scr-agents sessions``: ``list``, ``tidy`` or ``delete`` sessions.

    ``list`` prints each session with its runs, newest first; ``tidy`` moves legacy
    top-level runs into the ``legacy`` session; ``delete`` removes the given
    sessions or runs after a confirmation that ``--yes`` skips.

    Returns:
        0 on success. For ``delete``: 2 without ids, or without ``--yes`` when
        stdin is not a terminal; 1 if the prompt is declined or an id cannot be
        deleted.
    """
    from scr_agents.sessions import (
        DeleteError,
        delete_run,
        delete_session,
        list_sessions,
        sessions_root,
        tidy_legacy,
    )

    settings = _settings(args)
    if args.sessions_cmd == "delete":
        if not args.ids:
            print(
                "usage: scr-agents sessions delete <session-id|run-id> [...] [--yes]",
                file=sys.stderr,
            )
            return 2
        if not args.yes:
            if not sys.stdin.isatty():
                print(
                    "refusing to delete without confirmation; pass --yes",
                    file=sys.stderr,
                )
                return 2
            answer = input(
                f"Delete {', '.join(args.ids)} and everything in them from "
                f"{sessions_root(settings)}? [y/N] "
            )
            if answer.strip().lower() not in ("y", "yes"):
                print("nothing deleted")
                return 1
        status = 0
        for ident in args.ids:
            try:
                out = (
                    delete_run(settings, ident)
                    if ident.startswith("run-")
                    else delete_session(settings, ident)
                )
            except DeleteError as e:
                print(f"{ident}: {e}", file=sys.stderr)
                status = 1
                continue
            what = f"session with {len(out['runs'])} run(s)" if "runs" in out else "run"
            print(f"deleted {ident} ({what})")
        if status == 0:
            print(
                "a running console keeps a conversation it has open in memory "
                "until it restarts"
            )
        return status
    if args.sessions_cmd == "tidy":
        moved = tidy_legacy(settings)
        print(
            (
                f"moved {len(moved)} legacy run(s) into "
                f"{sessions_root(settings) / 'legacy'}"
            )
            if moved
            else "nothing to tidy"
        )
        return 0
    for s in list_sessions(settings):
        extra = s.get("repo") or ""
        print(
            f"{s['session_id']:18s} {s.get('kind', '?'):7s} "
            f"{(s.get('created_at') or '')[:19]:19s} "
            f"{len(s['runs'])} run(s)  {extra}"
        )
        for rid in s["runs"]:
            print(f"    {rid}")
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    """Handle ``scr-agents memory``: show the project memory or add a note to it.

    ``show`` prints the notes, the last recorded runs and the overrides kept in
    ``<repo>/.scr_agents/``; ``note`` appends the text under ``--section``.

    Returns:
        Always 0.
    """
    import json

    from scr_agents.inputs.project import resolve_project
    from scr_agents.ws5_knowledge.project_memory import ProjectMemory

    settings = _settings(args)
    memory = ProjectMemory(resolve_project(args.repo, Path(settings.projects_dir)))
    if args.memory_cmd == "note":
        p = memory.append_note(
            " ".join(args.text), section=args.section, author="human"
        )
        print(f"noted in {p}")
        return 0
    s = memory.summary()
    print(f"memory dir: {s['dir']} ({'exists' if s['exists'] else 'not created yet'})")
    print(s["notes"] or "(no notes)")
    if s["runs"]:
        print(
            f"\nruns recorded: {len(s['runs'])}, "
            f"findings: {s['findings_recorded']}, "
            f"human rejected: {len(s['human_rejected'])}"
        )
        for r in s["runs"][-5:]:
            print(
                f"  {r['run_id']} {r['ts'][:19]} {r['range']} {json.dumps(r['counts'])}"
            )
    if s["overrides"]:
        print("overrides: " + json.dumps(s["overrides"]))
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    """Handle ``scr-agents skills``: list the loaded skills, grouped by agent folder.

    Prints each skill's state, languages, categories, description and path; load
    errors go to stderr.

    Returns:
        Always 0.
    """
    from scr_agents.ws5_knowledge.skills import FOLDERS, LABELS, SkillLibrary

    settings = _settings(args)
    lib = SkillLibrary(settings.skill_paths())
    print(f"skill directories: {', '.join(str(p.resolve()) for p in lib.paths)}")
    for folder, component in FOLDERS.items():
        group = [s for s in lib.skills if s.component == component]
        if not group:
            continue
        print(f"\n{folder}/  {LABELS[component]}")
        for s in group:
            scope = ", ".join(
                [
                    *(s.languages or ["any language"]),
                    *(s.categories or ["any category"]),
                ]
            )
            print(
                f"  {s.name:24s} {'on ' if s.enabled else 'off'} [{scope}]  "
                f"{s.description}\n  {'':24s}     {s.path}"
            )
    if not lib.skills:
        print("(no skills loaded; see skills/README.md for the format)")
    for err in lib.errors:
        print(f"error: {err}", file=sys.stderr)
    return 0


def cmd_kb(args: argparse.Namespace) -> int:
    """Handle ``scr-agents kb``: print the knowledge base entries matching the query.

    Returns:
        Always 0.
    """
    from scr_agents.ws5_knowledge.knowledge_base import KnowledgeBase

    settings = _settings(args)
    kb = KnowledgeBase([Path(p) for p in settings.knowledge.paths])
    for e in kb.lookup(" ".join(args.query), limit=args.limit):
        print(f"{e.id:22s} [{e.standard}] {e.title}\n    {e.summary}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the ``scr-agents`` argument parser with every subcommand.

    The global options (``--config``, ``--mode``, ``--orchestrator``, ``--out``,
    ``-v``) are accepted both before and after the subcommand. Each subcommand
    stores its handler in ``func``.
    """

    def _common(parser: argparse.ArgumentParser, suppress: bool) -> None:
        d = argparse.SUPPRESS if suppress else None
        parser.add_argument(
            "--config", default=d, help="YAML config overriding the packaged defaults"
        )
        parser.add_argument(
            "--mode",
            default=d,
            choices=["auto", "local", "openai", "anthropic", "offline"],
            help="LLM mode: auto picks the local server if it answers, else "
            "offline; openai and anthropic are online APIs and only run when named",
        )
        parser.add_argument(
            "--orchestrator",
            default=d,
            choices=["deterministic", "openflow"],
            help="who orchestrates a review round: code (deterministic) or an LLM "
            "with tools (openflow); default review.orchestrator",
        )
        parser.add_argument("--out", default=d, help="output directory (default: out)")
        parser.add_argument(
            "-v",
            "--verbose",
            action="store_true",
            default=argparse.SUPPRESS if suppress else False,
        )

    p = argparse.ArgumentParser(
        prog="scr-agents", description="Multi-agent code review"
    )
    _common(p, suppress=False)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, **kw: object) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, **kw)  # type: ignore[arg-type]
        _common(sp, suppress=True)  # same options accepted after the subcommand
        return sp

    r = add(
        "review", help="review a commit range (and optional PR) in a git repo or URL"
    )
    r.add_argument("--repo", default=".", help="local path, git URL or owner/repo")
    r.add_argument("--base", required=True, help="base ref (e.g. main or a sha)")
    r.add_argument("--head", default="HEAD")
    r.add_argument("--pr", help="PR source: path to a JSON file or gh:<number>")
    r.add_argument("--task", choices=["find_bugs", "verify_fix"], default="find_bugs")
    r.add_argument("--claim", help="for verify_fix: what the change claims to fix")
    r.add_argument(
        "--scope",
        choices=["range", "all"],
        default="range",
        help="all = whole repository at head",
    )
    r.add_argument("--instructions", help="what to focus on, in your words")
    r.add_argument(
        "--live", action="store_true", help="render agent activity live in the terminal"
    )
    r.add_argument(
        "--fail-on",
        choices=["none", "confirmed", "unverified"],
        default="none",
        help=(
            "exit 1 when findings of this verdict (or stronger) exist; "
            "an incomplete review exits 2"
        ),
    )
    r.add_argument(
        "--auto-rounds",
        action="store_true",
        help=(
            "no one to ask: review the findings the validator could not verify "
            "again, up to review.max_rounds rounds"
        ),
    )
    r.set_defaults(func=cmd_review)

    n = add(
        "next-round",
        help=(
            "review a run's pending findings again (after recording verdicts), "
            "with its Review Report and Validator Report as context"
        ),
    )
    n.add_argument("--run", help="run id (looked up under out/sessions)")
    n.add_argument("--run-dir", help="explicit run directory (alternative to --run)")
    n.set_defaults(func=cmd_next_round)

    c = add(
        "chat",
        help=(
            "interactive session on a project path or git URL "
            "(Claude Code-style live view)"
        ),
    )
    c.add_argument("target", help="local path, git URL or owner/repo")
    c.add_argument("--base")
    c.add_argument("--head")
    c.add_argument("--scope", choices=["range", "all"])
    c.add_argument(
        "--ask", nargs="+", help="run one request non-interactively and exit"
    )
    c.set_defaults(func=cmd_chat)

    d = add("demo", help="create a demo repo with planted defects and review it")
    d.add_argument("--dir", help="where to create the demo repo (default: temp dir)")
    d.add_argument(
        "--auto-rounds",
        action="store_true",
        help="review unverified findings again, up to review.max_rounds rounds",
    )
    d.set_defaults(func=cmd_demo)

    v = add("verdict", help="record a human verdict for a finding (feedback loop)")
    v.add_argument("--run", help="run id (looked up under out/sessions)")
    v.add_argument("--run-dir", help="explicit run directory (alternative to --run)")
    v.add_argument("--finding", required=True)
    v.add_argument("--verdict", choices=["accept", "reject"], required=True)
    v.add_argument("--note")
    v.set_defaults(func=cmd_verdict)

    g = add("registry", help="approved prompt/skill registry")
    gs = g.add_subparsers(dest="registry_cmd", required=True)
    gs.add_parser("list")
    s = gs.add_parser("sign", help="vet a prompt file: record its hash in the manifest")
    s.add_argument("name")
    s.add_argument("path")
    s.add_argument("--pending", action="store_true", help="register without approving")
    s.add_argument("--version", default="1")
    s.add_argument("--variant", default="A")
    s.add_argument("--description")
    gs.add_parser("stats", help="human-verdict statistics per prompt variant")
    g.set_defaults(func=cmd_registry)

    w = add(
        "serve", help="start the web console (live view of agents, tools and findings)"
    )
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8765)
    w.set_defaults(func=cmd_serve)

    m = add(
        "llm",
        help=(
            "check the local LLM server (vLLM / SGLang) "
            "for tool calling and JSON output"
        ),
    )
    m.add_argument("llm_cmd", choices=["check"])
    m.add_argument("--base-url", help="override the base_url of the mode in use")
    m.add_argument("--model", help="override the model of the mode in use")
    m.set_defaults(func=cmd_llm)

    mc = add(
        "mcp",
        help=(
            "check the configured MCP servers: start them, list tools, probe web search"
        ),
    )
    mc.add_argument("mcp_cmd", choices=["check"])
    mc.add_argument(
        "--query",
        help="run this query through web_search and print the filtered result",
    )
    mc.set_defaults(func=cmd_mcp)

    ss = add("sessions", help="list, delete or tidy sessions and their runs")
    ss.add_argument(
        "sessions_cmd", choices=["list", "tidy", "delete"], nargs="?", default="list"
    )
    ss.add_argument(
        "ids", nargs="*", help="for delete: session ids (conversations) or run ids"
    )
    ss.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="for delete: do not ask for confirmation",
    )
    ss.set_defaults(func=cmd_sessions)

    mm = add("memory", help="project memory stored in <repo>/.scr_agents/")
    mm.add_argument("memory_cmd", choices=["show", "note"], nargs="?", default="show")
    mm.add_argument("--repo", default=".", help="local path, git URL or owner/repo")
    mm.add_argument(
        "--section",
        default="Notes",
        choices=["Build and test", "Conventions", "Known false positives", "Notes"],
    )
    mm.add_argument("text", nargs="*", help="note text (for `note`)")
    mm.set_defaults(func=cmd_memory)

    sk = add(
        "skills", help="list the skills (skills/<name>/SKILL.md) the agents can read"
    )
    sk.set_defaults(func=cmd_skills)

    k = add("kb", help="query the knowledge base")
    k.add_argument("query", nargs="+")
    k.add_argument("--limit", type=int, default=5)
    k.set_defaults(func=cmd_kb)
    return p


def load_env() -> None:
    """Read ``.env`` from the working directory (GPUStack keys, MLflow settings).

    Variables already set win. ``SCR_AGENTS_NO_DOTENV`` turns it off (the tests set
    it).
    """
    if os.environ.get("SCR_AGENTS_NO_DOTENV"):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv comes with the "agents" extra
        return
    load_dotenv(Path.cwd() / ".env", override=False)


def main(argv: list[str] | None = None) -> int:
    """Run the ``scr-agents`` command line and return the process exit code.

    Args:
        argv: Arguments without the program name; ``None`` reads ``sys.argv``.

    Returns:
        The subcommand's exit code, 130 on Ctrl-C, or 1 if it raised (with ``-v``
        the exception propagates instead).
    """
    load_env()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as e:
        if args.verbose:
            raise
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
