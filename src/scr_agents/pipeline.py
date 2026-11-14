"""Pipeline: wires the workstreams together in the order of the architecture diagram."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scr_agents.config import ORCHESTRATORS, Settings
from scr_agents.inputs.request import build_request
from scr_agents.llm.backend import AnthropicBackend, LLMBackend, OfflineBackend
from scr_agents.llm.local import LocalBackend
from scr_agents.llm.mcp_client import MCPClient
from scr_agents.llm.mcp_registry import effective as mcp_effective
from scr_agents.llm.server_metrics import CacheMonitor
from scr_agents.models import EvidenceReport, ReviewRequest, Task
from scr_agents.sessions import ensure_session, new_session
from scr_agents.ws1_review.openflow import OpenFlowAgent
from scr_agents.ws1_review.orchestrator import ReviewAgent
from scr_agents.ws1_review.rounds import (
    drop_settled,
    next_request,
    prepare_round,
    round_status,
    run_status,
)
from scr_agents.ws1_review.tools import ToolContext, agent_tool_name
from scr_agents.ws1_review.validator import ValidatorAgent
from scr_agents.ws2_analysis.code_graph import CodeGraph
from scr_agents.ws2_analysis.scanners import run_scanners
from scr_agents.ws3_harness.evidence import write_report
from scr_agents.ws3_harness.report_rounds import write_round_reports
from scr_agents.ws3_harness.shell import SandboxedShell, ensure_tool_path
from scr_agents.ws3_harness.test_bench import TestBench
from scr_agents.ws3_harness.workspace import Workspace
from scr_agents.ws4_guardrails.registry import PromptRegistry
from scr_agents.ws4_guardrails.safety_filter import SafetyFilter
from scr_agents.ws4_guardrails.web_search import WebSearch
from scr_agents.ws5_knowledge.activity_log import ActivityLog
from scr_agents.ws5_knowledge.human_review import write_packet
from scr_agents.ws5_knowledge.knowledge_base import KnowledgeBase
from scr_agents.ws5_knowledge.project_memory import MEMORY_DIRS, ProjectMemory
from scr_agents.ws5_knowledge.skills import SkillLibrary

log = logging.getLogger("scr_agents")

BackendFactory = Callable[[str, str | None], LLMBackend]


def resolve_mode(settings: Settings) -> str:
    """Return the LLM mode to run in, resolving ``auto`` to local or offline.

    Auto -> local when the configured server answers, otherwise offline. Never the
    online API.
    """
    if settings.llm.mode == "auto":
        return (
            "local"
            if LocalBackend.reachable(
                settings.llm.local.base_url, settings.llm.local.api_key
            )
            else "offline"
        )
    return settings.llm.mode


def local_models(settings: Settings) -> tuple[str, str, str]:
    """Resolve the reviewer model, validator model and validator URL for local mode.

    An empty ``llm.local.model`` picks the first served model that matches
    ``preferred_models``; empty validator settings fall back to the reviewer's.

    Returns:
        ``(reviewer_model, validator_model, validator_base_url)``.

    Raises:
        RuntimeError: If no model is configured and the server lists none.
    """
    cfg = settings.llm.local
    model = cfg.model
    if not model:
        served = LocalBackend.list_models(cfg.base_url, cfg.api_key)
        if not served:
            raise RuntimeError(
                f"local LLM at {cfg.base_url} lists no models; set llm.local.model"
            )
        model = pick_model(served, cfg.preferred_models)
    return model, cfg.validator_model or model, cfg.validator_base_url or cfg.base_url


def independent_validator(
    reviewer_model: str, validator_model: str, reviewer_url: str, validator_url: str
) -> bool:
    """Return whether the validator differs from the reviewer in model or server.

    The architecture wants a second model to re-check findings; the same model on the
    same server is not one, however the config spells it.
    """
    return reviewer_model != validator_model or reviewer_url.rstrip(
        "/"
    ) != validator_url.rstrip("/")


def pick_model(served: list[str], preferred: list[str]) -> str:
    """First served model whose id contains a preferred name, else the first served.

    The match is case-insensitive, and earlier preferences win.
    """
    low = [(m, m.lower()) for m in served]
    for pref in preferred:
        p = pref.lower()
        for m, ml in low:
            if p in ml:
                return m
    return served[0]


def make_backend_factory(settings: Settings) -> tuple[BackendFactory, str]:
    """Resolve the LLM mode and build the factory that gives each agent its backend.

    Returns:
        ``(factory, mode)``. ``factory(role, focus)`` returns an ``OfflineBackend``,
        a ``LocalBackend`` or an ``AnthropicBackend`` according to ``mode``. The
        ``validator`` role gets the validator model (and, in local mode, its
        server); in Anthropic mode the ``planner`` role gets ``planner_model``. In
        local mode each backend's context window is the ``max_model_len`` its
        server reports for that model, else ``llm.local.context_window``.

    Raises:
        RuntimeError: In local mode, if no model is configured and the server lists
            none.
    """
    mode = resolve_mode(settings)
    llm = settings.llm
    resolved = local_models(settings) if mode == "local" else None
    windows: dict[tuple[str, str], int] = {}
    if resolved is not None:
        model, vmodel, vurl = resolved
        for url, name in {(llm.local.base_url, model), (vurl, vmodel)}:
            served = LocalBackend.served_context_window(url, name, llm.local.api_key)
            windows[(url, name)] = served or llm.local.context_window
            log.info(
                "context window of %s at %s: %d tokens (%s)",
                name,
                url,
                windows[(url, name)],
                "max_model_len" if served else "llm.local.context_window",
            )

    def factory(role: str, focus: str | None = None) -> LLMBackend:
        if mode == "offline":
            return OfflineBackend(role, focus)
        if mode == "local":
            cfg = llm.local
            model, vmodel, vurl = resolved  # type: ignore[misc]
            base_url, chosen = (
                (vurl, vmodel) if role == "validator" else (cfg.base_url, model)
            )
            return LocalBackend(
                base_url,
                chosen,
                api_key=cfg.api_key,
                temperature=cfg.temperature,
                max_tokens=cfg.max_tokens,
                timeout_s=cfg.timeout_s,
                json_repair=cfg.json_repair,
                extra_body=cfg.extra_body,
                context_window=windows[(base_url, chosen)],
            )
        if role == "validator":
            model, effort = llm.validator_model, llm.validator_effort
        elif role == "planner":
            model, effort = llm.planner_model or llm.reviewer_model, llm.effort
        else:
            model, effort = llm.reviewer_model, llm.effort
        return AnthropicBackend(
            model=model,
            effort=effort,
            server_fallbacks=llm.server_fallbacks,
            thinking_display=llm.thinking_display,
        )

    return factory, mode


def run_review(
    settings: Settings,
    repo: str | Path,
    base: str,
    head: str = "HEAD",
    pr: str | None = None,
    task: Task | str = Task.FIND_BUGS,
    claim: str | None = None,
    request: ReviewRequest | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    session_id: str | None = None,
    previous_run: Path | None = None,
    auto_rounds: bool = False,
) -> tuple[EvidenceReport, Path]:
    """Run one review end to end and write the results into its run directory.

    Builds the request unless one is given, loads project memory, prompts, knowledge
    and skills, picks the LLM backend, runs the code graph, scanners, test bench,
    review agent and validator, then writes the report and the human review packet
    to ``<session dir>/<run_id>/``.

    With ``previous_run`` the run is the next review round of that run: the planner
    and the subagents get its pending findings and the validator's comments on them,
    its settled findings are carried into this report, and new reports that repeat a
    settled finding are left out (see ``ws1_review.rounds``). Each round also writes
    ``report_a.html`` (its review report with the validator's comments) and
    ``report_b.html`` (the validator's comment report).

    Args:
        settings: Loaded settings; the MCP servers switched on in ``mcp_servers/`` are
            added to them.
        repo: Path of the repository to review.
        base: Base ref of the range.
        head: Head ref of the range.
        pr: Optional PR source: a JSON file or ``gh:<number>``.
        task: What the reviewers are asked to do.
        claim: For ``verify_fix``, what the change claims to fix.
        request: A prepared request, used instead of building one from ``repo``,
            ``base``, ``head``, ``pr``, ``task`` and ``claim``.
        on_event: Called with every activity log record, for live views.
        session_id: Session to run in; defaults to the request's session, else a
            new ``cli`` session.
        previous_run: Run directory of the round before this one, if this run is a
            later round; build the request with ``rounds.next_request``.
        auto_rounds: Settle findings by the validator's verdicts alone, for runs with
            no person to ask (``--auto-rounds``); a later round keeps the setting
            of the first, and so does ``review.orchestrator``.

    Returns:
        The evidence report and the run directory. Agents that failed are listed
        in ``report.errors`` and leave the report incomplete instead of failing
        the run.

    Raises:
        RoundError: If ``previous_run`` passed or reached ``review.max_rounds``;
            nothing is written then.
        Exception: Whatever stopped the run, after it is logged as ``run_error``.
    """
    t_start = time.monotonic()
    ensure_tool_path()  # scanners and test runners installed next to the interpreter
    prior = (
        prepare_round(previous_run, settings.review.max_rounds)
        if previous_run is not None
        else None
    )
    if prior is not None:
        auto_rounds = prior.previous.auto_rounds
        first = prior.previous.stats.get("orchestrator")
        if first in ORCHESTRATORS and first != settings.review.orchestrator:
            settings = settings.model_copy(deep=True)
            settings.review.orchestrator = first
    round_no = prior.round if prior is not None else 1
    # the MCP servers switched on in mcp_servers/ (console or mcp_servers/config.yaml)
    settings = mcp_effective(settings)
    request = request or build_request(repo, base, head, pr=pr, task=task, claim=claim)
    session_id = (
        session_id or request.session_id or new_session(settings, "cli", repo=str(repo))
    )
    request.session_id = session_id
    run_dir = ensure_session(settings, session_id) / request.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "request.json").write_text(request.model_dump_json(indent=2))

    activity = ActivityLog(
        run_dir / "activity.jsonl",
        request.run_id,
        request.commit_ids,
        subscribers=[on_event] if on_event else None,
    )

    def stage(name: str, status: str, **extra: Any) -> None:
        activity.log("stage", stage=name, status=status, **extra)

    activity.log(
        "request",
        files=len(request.files),
        commits=len(request.commits),
        task=request.task.value,
        base=request.base,
        head=request.head,
        pr=request.pr.number if request.pr else None,
        paths=[f.path for f in request.files],
        repo=request.repo_path,
        claim=request.claim,
        session_id=session_id,
    )
    stage("inputs", "done", files=len(request.files), commits=len(request.commits))
    activity.log(
        "round",
        round=round_no,
        max_rounds=settings.review.max_rounds,
        previous_run=prior.previous.run_id if prior is not None else None,
        carried=[vf.finding.id for vf in prior.settled] if prior is not None else [],
        pending=[vf.finding.id for vf in prior.pending] if prior is not None else [],
        auto=auto_rounds,
    )
    log.info(
        "run %s: %d files, %d commits (%s..%s)",
        request.run_id,
        len(request.files),
        len(request.commits),
        request.base[:10],
        request.head[:10],
    )

    mcp = None
    cache_monitor = None
    try:
        # The change under review must not be able to rewrite the memory that
        # suppresses findings or picks build commands: when the range touches
        # .scr_agents/, read it from the base commit.
        touched = any(
            f.path == d or f.path.startswith(d + "/")
            for f in request.files
            for d in MEMORY_DIRS
        )
        memory = ProjectMemory(
            request.repo_path, read_ref=request.base if touched else None
        )
        overrides = memory.overrides()
        if overrides.bench.build_cmd or overrides.bench.test_cmd:
            settings = settings.model_copy(deep=True)
            settings.bench.build_cmd = (
                overrides.bench.build_cmd or settings.bench.build_cmd
            )
            settings.bench.test_cmd = (
                overrides.bench.test_cmd or settings.bench.test_cmd
            )
        activity.log(
            "memory",
            exists=memory.exists,
            notes_chars=len(memory.notes()),
            history=len(memory.history()),
            overrides=sorted(overrides.model_dump(exclude_defaults=True)),
            dir=str(memory.dir),
            read_from="base" if touched else "worktree",
        )
        if memory.errors:
            activity.log("memory_error", error="; ".join(memory.errors))
        registry = PromptRegistry(
            settings.manifest_path(),
            settings.registry.require_approved,
            stats_path=Path(settings.output_dir) / "registry_stats.jsonl",
        )
        kb = KnowledgeBase([Path(p) for p in settings.knowledge.paths])
        skills = SkillLibrary(settings.skill_paths())
        activity.log(
            "skills",
            count=len(skills.skills),
            names=[s.name for s in skills.skills],
            paths=[str(p) for p in skills.paths],
            errors=skills.errors,
            disabled=[s.name for s in skills.skills if not s.enabled],
            components={s.name: s.component for s in skills.skills},
        )
        for err in skills.errors:
            log.warning("skill not loaded: %s", err)
        safety = SafetyFilter(settings.safety)
        factory, mode = make_backend_factory(settings)
        models = {"reviewer": mode, "validator": mode}  # recorded in the report's stats
        if mode == "local":
            rm, vm, vurl = local_models(settings)
            models = {"reviewer": rm, "validator": vm}
            indep = independent_validator(rm, vm, settings.llm.local.base_url, vurl)
            activity.log(
                "mode",
                mode=mode,
                reviewer_model=rm,
                validator_model=vm,
                base_url=settings.llm.local.base_url,
                independent_validator=indep,
            )
            if not indep:
                log.warning(
                    "validator and reviewer are the same model (%s); set "
                    "llm.local.validator_model (and validator_base_url) to a second "
                    "served model for an independent re-check",
                    vm,
                )
            if settings.llm.local.cache_metrics_interval_s > 0:
                cache_monitor = CacheMonitor(
                    [
                        {
                            "base_url": settings.llm.local.base_url,
                            "model": rm,
                            "role": "reviewer",
                        },
                        {"base_url": vurl, "model": vm, "role": "validator"},
                    ],
                    activity,
                    api_key=settings.llm.local.api_key,
                    interval_s=settings.llm.local.cache_metrics_interval_s,
                ).start()
        elif mode == "anthropic":
            models = {
                "reviewer": settings.llm.reviewer_model,
                "validator": settings.llm.validator_model,
            }
            activity.log(
                "mode",
                mode=mode,
                reviewer_model=settings.llm.reviewer_model,
                validator_model=settings.llm.validator_model,
                independent_validator=settings.llm.reviewer_model
                != settings.llm.validator_model,
            )
        else:
            activity.log("mode", mode=mode, reviewer_model=mode, validator_model=mode)
        log.info("LLM mode: %s", mode)

        if settings.mcp_servers:
            if MCPClient.available():
                mcp = MCPClient(
                    settings.mcp_servers, log=activity, safety=safety
                ).start()
                for name, err in mcp.errors.items():
                    log.warning("MCP server %s failed to start: %s", name, err)
                    activity.log("mcp_error", server=name, error=err)
                servers = mcp.status()
                comps = {s.name: s.components for s in settings.mcp_servers}
                for srv in servers:
                    srv["components"] = (
                        comps.get(srv["name"]) or []
                    )  # empty = every agent that takes MCP tools
                    for t in srv["tools"]:
                        t["agent_tool"] = agent_tool_name(
                            settings, srv["name"], t["name"]
                        )
            else:
                log.warning(
                    "mcp_servers configured but the `mcp` package is missing "
                    "(pip install 'mcp[cli]')"
                )
                servers = [
                    {
                        "name": s.name,
                        "command": Path(s.command).name,
                        "untrusted": s.untrusted,
                        "tools": [],
                        "status": "failed",
                        "error": (
                            "the `mcp` package is not installed (uv sync --extra mcp)"
                        ),
                    }
                    for s in settings.mcp_servers
                ]
            activity.log(
                "mcp_servers",
                servers=servers,
                web_search=settings.web_search.mcp_server
                if settings.web_search.enabled
                else None,
            )

        with Workspace(request, run_dir) as ws:
            # WS2: code graph and static scanners on the head checkout
            stage("graph", "running")
            graph = CodeGraph.build(ws.head_dir)
            graph_report = graph.blast_radius(request.files)
            activity.log(
                "code_graph",
                symbols=len(graph_report.changed_symbols),
                impacted_files=len(graph_report.impacted_files),
                summary=graph_report.summary,
                changed_symbols=[
                    s.model_dump() for s in graph_report.changed_symbols[:50]
                ],
            )
            stage(
                "graph",
                "done",
                symbols=len(graph_report.changed_symbols),
                impacted_files=len(graph_report.impacted_files),
            )
            stage("scanners", "running")
            scanner_report = run_scanners(settings.scanners, ws.head_dir, request.files)
            if memory.exists:
                kept = [
                    f
                    for f in scanner_report.findings
                    if not memory.is_ignored(f.path, f"{f.scanner}:{f.rule}")
                ]
                ignored = len(scanner_report.findings) - len(kept)
                if ignored:
                    scanner_report.findings = kept
                    scanner_report.errors.append(
                        f"{ignored} finding(s) ignored by .scr_agents/project.yaml"
                    )
                    activity.log("memory_ignored", count=ignored)
            activity.log(
                "scanners",
                run=scanner_report.scanners_run,
                missing=scanner_report.scanners_missing,
                findings=len(scanner_report.findings),
                errors=scanner_report.errors,
                items=[f.model_dump() for f in scanner_report.findings[:100]],
            )
            stage(
                "scanners",
                "done",
                findings=len(scanner_report.findings),
                run=scanner_report.scanners_run,
            )
            log.info("graph: %s", graph_report.summary)
            log.info(
                "scanners: %s ran, %d findings, missing %s",
                scanner_report.scanners_run,
                len(scanner_report.findings),
                scanner_report.scanners_missing,
            )

            # WS3: harness
            stage("bench", "running")
            shell = SandboxedShell(settings.shell, ws.root)
            bench = TestBench(settings.bench, settings.shell, ws)
            bench_report = bench.run_both()
            stage("bench", "done", delta=bench_report.delta)
            activity.log(
                "test_bench",
                delta=bench_report.delta,
                base=bench_report.base.model_dump(exclude={"stdout", "stderr"})
                if bench_report.base
                else None,
                head=bench_report.head.model_dump(exclude={"stdout", "stderr"})
                if bench_report.head
                else None,
            )
            log.info("test bench: %s", bench_report.delta)

            ctx = ToolContext(
                settings=settings,
                request=request,
                workspace=ws,
                graph=graph,
                graph_report=graph_report,
                scanner_report=scanner_report,
                kb=kb,
                safety=safety,
                shell=shell,
                bench=bench,
                bench_report=bench_report,
                web_search=WebSearch(settings.web_search, mcp, safety),
                mcp=mcp,
                memory=memory,
                skills=skills,
                prior_round=prior,
            )

            orchestrator = settings.review.orchestrator
            agent_stats: dict[str, Any]
            if orchestrator == "openflow":
                # WS1, OpenFlow: an LLM runs the reviewers and the validator as tools
                stage("review", "running", orchestrator=orchestrator)
                flow = OpenFlowAgent(settings, registry, activity, factory)
                plan, validated = flow.run(ctx)
                log.info("openflow: %d findings", len(validated))
                stage("review", "done", candidates=len(flow.candidates))
                stage("validation", "done")
                agent_stats, errors = flow.stats(), list(flow.errors)
            else:
                # WS1: review agent -> subagents -> validator
                stage("review", "running", orchestrator=orchestrator)
                reviewer = ReviewAgent(settings, registry, activity, factory)
                plan, candidates = reviewer.run(ctx)
                if prior is not None:
                    candidates, repeats = drop_settled(candidates, prior.settled)
                    if repeats:
                        activity.log(
                            "round_repeats",
                            "review_agent",
                            count=len(repeats),
                            ids=repeats,
                        )
                log.info("review agent: %d candidate findings", len(candidates))
                stage("review", "done", candidates=len(candidates))
                stage("validation", "running", candidates=len(candidates))
                validator = ValidatorAgent(settings, registry, activity, factory)
                validated = validator.validate(ctx, candidates)
                stage("validation", "done")
                agent_stats = {
                    **reviewer.stats(),
                    **validator.stats(),
                    "prompts_used": sorted(
                        set(reviewer.prompts_used) | validator.prompts_used
                    ),
                }
                errors = [*reviewer.errors, *validator.errors]
            for vf in validated:
                vf.round = round_no

            stats = {
                "mode": mode,
                "orchestrator": orchestrator,
                "duration_s": round(time.monotonic() - t_start, 1),
                "scanners_run": scanner_report.scanners_run,
                **agent_stats,
            }
            stats["models"] = models
            report = EvidenceReport(
                run_id=request.run_id,
                request=request,
                plan=plan,
                findings=[*(prior.settled if prior is not None else []), *validated],
                scanner=scanner_report,
                graph=graph_report,
                bench=bench_report,
                stats=stats,
                errors=errors,
                round=round_no,
                max_rounds=settings.review.max_rounds,
                previous_run_id=prior.previous.run_id if prior is not None else None,
                replaced=[vf.finding.id for vf in prior.pending]
                if prior is not None
                else [],
                auto_rounds=auto_rounds,
            )
            if errors:
                log.warning("review incomplete: %s", "; ".join(errors))
            try:
                # carried findings were recorded by the round that found them
                recorded = memory.record_run(
                    report.model_copy(update={"findings": validated}), session_id
                )
                activity.log("memory_recorded", findings=recorded, dir=str(memory.dir))
            except OSError as e:
                # read-only checkout or a symlinked .scr_agents/: memory is optional
                activity.log("memory_error", error=str(e))
            md, js = write_report(report, run_dir)
            packet = write_packet(report, run_dir)
            page_a, page_b = write_round_reports(report, run_dir)
            activity.log(
                "report",
                counts=report.counts(),
                markdown=str(md),
                json=str(js),
                packet=str(packet),
                incomplete=report.incomplete,
                errors=errors,
                round=round_no,
                report_a=str(page_a),
                report_b=str(page_b),
            )
            activity.log(
                "round_status",
                **round_status(report, []).model_dump(exclude={"run_id"}),
                report_a=str(page_a),
                report_b=str(page_b),
            )
            stage("report", "done", counts=report.counts())
            log.info("report: %s (%s)", md, report.counts())
            return report, run_dir
    except Exception as e:
        activity.log("run_error", error=f"{type(e).__name__}: {e}")
        raise
    finally:
        if cache_monitor is not None:
            cache_monitor.stop()
        if mcp is not None:
            mcp.close()
        activity.close()


def run_next_round(
    settings: Settings,
    previous_run: Path,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[EvidenceReport, Path]:
    """Run the next review round of the run in ``previous_run``, in its session.

    It reviews the same change again, focused on the findings that round left
    pending; see ``run_review``.

    Raises:
        RoundError: If that round passed or the round limit is reached.
    """
    previous = EvidenceReport.model_validate_json(
        (previous_run / "report.json").read_text()
    )
    r = previous.request
    return run_review(
        settings,
        r.repo_path,
        r.base,
        r.head,
        request=next_request(previous),
        on_event=on_event,
        session_id=r.session_id,
        previous_run=previous_run,
    )


def run_auto_rounds(
    settings: Settings,
    report: EvidenceReport,
    run_dir: Path,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[EvidenceReport, Path]:
    """Run more rounds after an ``auto_rounds`` run while findings stay unverified.

    Stops when every finding is settled or ``review.max_rounds`` rounds have run.

    Returns:
        The report and run directory of the last round.
    """
    while run_status(run_dir, settings.review.max_rounds).can_continue:
        report, run_dir = run_next_round(settings, run_dir, on_event)
    return report, run_dir
