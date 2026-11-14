"""Review agent: plans, fans out to the subagents in parallel, then ranks candidates.

It turns the subagents' output into ranked candidates for the validator: nearby reports
of the same spot merge into one finding that remembers every source (agreement), scanner
hits either strengthen a finding or, when high-severity and unreported, become
candidates themselves, each candidate is grounded against the head checkout (path
exists, line inside the file, near the change) and quoted, and the list is ranked by
grounding, agreement, severity, reproducibility and confidence.
"""

from __future__ import annotations

import json
import re
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from scr_agents.config import Settings
from scr_agents.llm.backend import LLMBackend
from scr_agents.llm.loop import ToolLoop
from scr_agents.models import (
    Category,
    Finding,
    Location,
    ReviewPlan,
    Severity,
    split_lines,
)
from scr_agents.ws1_review.rounds import round_section
from scr_agents.ws1_review.subagents import SUBAGENTS, run_subagent
from scr_agents.ws1_review.tools import ToolContext, _safe_path
from scr_agents.ws4_guardrails.registry import PromptRegistry
from scr_agents.ws5_knowledge.activity_log import ActivityLog
from scr_agents.ws5_knowledge.skills import inline_skills

BackendFactory = Callable[[str, str | None], LLMBackend]

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "focus_files": {"type": "array", "items": {"type": "string"}},
        "hypotheses": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["focus_files", "hypotheses", "notes"],
    "additionalProperties": False,
}

_SEV_RANK = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}
NEAR_LINES = 3  # reports this close on the same file describe the same spot
NEAR_CHANGE = (
    10  # in range mode a finding this far from a changed line still counts as grounded
)


def _span(f: Finding) -> tuple[int, int] | None:
    line = f.location.line
    if line is None:
        return None
    end = f.location.end_line or line
    return min(line, end), max(line, end)


def _near(a: tuple[int, int], b: tuple[int, int], slack: int = NEAR_LINES) -> bool:
    return a[0] - slack <= b[1] and b[0] - slack <= a[1]


def _title_key(title: str) -> str:
    return re.sub(r"\W+", " ", title.lower()).strip()


def merge_findings(findings: list[Finding]) -> list[Finding]:
    """Cluster reports of the same spot into one finding each.

    The same spot is the same path with line ranges within NEAR_LINES (any category), or
    the same path, no line and the same title. The most confident report becomes the
    finding and remembers the others as `merged_from` / `agreement`; it is filed as a
    vulnerability when any report of the spot is one.
    """
    clusters: list[list[Finding]] = []
    for f in findings:
        for cluster in clusters:
            head = cluster[0]
            if head.location.path != f.location.path:
                continue
            sa, sb = _span(head), _span(f)
            if (sa and sb and _near(sa, sb)) or (
                sa is None
                and sb is None
                and _title_key(head.title) == _title_key(f.title)
            ):
                cluster.append(f)
                break
        else:
            clusters.append([f])
    out: list[Finding] = []
    for cluster in clusters:
        primary = max(
            cluster, key=lambda f: (f.confidence, -_SEV_RANK.get(f.severity, 9))
        )
        sources = sorted({f.source for f in cluster} | set(primary.merged_from))
        primary.merged_from, primary.agreement = sources, len(sources)
        primary.cwe = sorted({c for f in cluster for c in f.cwe})
        if any(f.category == Category.VULNERABILITY for f in cluster):
            # the most confident report may come from the bug reviewer; the spot is
            # still a security problem when another reviewer filed it as one
            primary.category = Category.VULNERABILITY
        primary.references = sorted({r for f in cluster for r in f.references})
        primary.scanner_rule = primary.scanner_rule or next(
            (f.scanner_rule for f in cluster if f.scanner_rule), None
        )
        primary.reproduction = primary.reproduction or next(
            (f.reproduction for f in cluster if f.reproduction), None
        )
        primary.suggested_fix = primary.suggested_fix or next(
            (f.suggested_fix for f in cluster if f.suggested_fix), None
        )
        out.append(primary)
    return out


def absorb_scanner_hits(findings: list[Finding], hits: list[Any]) -> list[Finding]:
    """A scanner hit next to a finding strengthens it (agreement, rule, CWEs).

    An unreported critical/high hit becomes a candidate of its own.
    """
    out = list(findings)
    for sf in hits:
        match = None
        if sf.line is not None:
            match = next(
                (
                    f
                    for f in out
                    if f.location.path == sf.path
                    and (span := _span(f)) is not None
                    and _near(span, (sf.line, sf.line))
                ),
                None,
            )
        if match is not None:
            src = f"scanner:{sf.scanner}"
            if src not in match.merged_from:
                match.merged_from = sorted([*match.merged_from, src])
                match.agreement = len(match.merged_from)
            match.scanner_rule = match.scanner_rule or f"{sf.scanner}:{sf.rule}"
            match.cwe = sorted(set(match.cwe) | set(sf.cwe))
            continue
        if sf.severity not in ("critical", "high"):
            continue
        cat = (
            Category(sf.category)
            if sf.category in Category.__members__.values()
            else Category.OTHER
        )
        out.append(
            Finding(
                title=f"{sf.scanner} {sf.rule}: {sf.message}",
                category=cat,
                severity=Severity(sf.severity),
                location=Location(path=sf.path, line=sf.line),
                description=sf.message,
                rationale="Static scanner hit not reported by any subagent; "
                "promoted for validation.",
                cwe=list(sf.cwe),
                source=f"scanner:{sf.scanner}",
                confidence=0.4,
                scanner_rule=f"{sf.scanner}:{sf.rule}",
                merged_from=[f"scanner:{sf.scanner}"],
            )
        )
    return out


def ground_findings(findings: list[Finding], ctx: ToolContext) -> None:
    """Check each finding against the head checkout and mark it grounded or not.

    The path must exist, the line must be inside the file (then it is quoted into
    `code_quote`) and, in range mode, close to a changed line.
    """
    changed = {f.path: f.changed_lines() for f in ctx.request.files}
    for f in findings:
        try:
            p = _safe_path(ctx.workspace.head_dir, f.location.path)
        except ValueError:
            p = None
        if p is None or not p.is_file():
            f.grounded = False
            continue
        lines = split_lines(p.read_text(encoding="utf-8", errors="replace"))
        line = f.location.line
        if line is None or line < 1 or line > len(lines):
            f.grounded = False
            continue
        f.code_quote = lines[line - 1].strip()[:200]
        if ctx.request.scope == "range":
            near = changed.get(f.location.path) or []
            f.grounded = any(abs(line - n) <= NEAR_CHANGE for n in near)
        else:
            f.grounded = True


def rank_key(f: Finding) -> tuple[Any, ...]:
    """Sort key for the validation order.

    Grounded first, then by agreement, severity, having a reproduction, confidence.
    """
    return (
        not f.grounded,
        -f.agreement,
        _SEV_RANK.get(f.severity, 9),
        0 if f.reproduction else 1,
        -f.confidence,
    )


def with_round_focus(plan: ReviewPlan, ctx: ToolContext) -> ReviewPlan:
    """In a later round, put the files of the findings to review again first."""
    if ctx.prior_round is None:
        return plan
    paths = [vf.finding.location.path for vf in ctx.prior_round.pending]
    plan.focus_files = list(dict.fromkeys([*paths, *plan.focus_files]))[:20]
    return plan


def change_overview(ctx: ToolContext, extra: str = "") -> str:
    """The brief of a change that the planner and the OpenFlow orchestrator start from.

    Task, scope, the user's instructions, the changed files, the blast radius, scanner
    counts, project memory notes, the round before (if any), ``extra`` (the planner's
    inline skills), commit subjects and the truncated diff; repository text is
    wrapped as untrusted.
    """
    req = ctx.request
    files = ctx.safety.wrap(
        "\n".join(
            f"- {f.path} [{f.status}] +{f.added}/-{f.removed}" for f in req.files
        ),
        "changed_files",
        6000,
    )
    scanners: dict[str, int] = {}
    for sf in ctx.scanner_report.findings:
        scanners[sf.category] = scanners.get(sf.category, 0) + 1
    memory_notes = ""
    if ctx.memory is not None and ctx.memory.notes().strip():
        memory_notes = (
            "Project memory notes:\n"
            f"{ctx.safety.wrap(ctx.memory.notes(), 'project_memory', 3000)}\n"
        )
    return (
        f"Task: {req.task.value}"
        + (f" (claim: {req.claim})" if req.claim else "")
        + (
            f"\nScope: whole repository ({len(req.files)} files)"
            if req.scope == "all"
            else ""
        )
        + (f"\nRequested by the user: {req.instructions}" if req.instructions else "")
        + "\n"
        + f"Changed files:\n{files}\n\nBlast radius: {ctx.graph_report.summary}\n"
        + f"Scanner findings by category: {scanners or 'none'}\n"
        + memory_notes
        + round_section(ctx.prior_round, ctx.safety)
        + extra
        + f"Commits:\n{
            ctx.safety.wrap(
                chr(10).join(c.subject for c in req.commits[:30]),
                'commit_subjects',
                4000,
            )
        }\n"
        + "Diff (truncated):\n"
        f"{ctx.safety.wrap(req.diff_text(12000), 'code_diff', 12500)}\n\n"
    )


class ReviewAgent:
    """Plan the review, run the subagents in parallel and rank their candidates.

    LLM calls, token usage, the prompts used and agent failures are collected for the
    run report; a planner or subagent that fails is recorded in ``errors`` and the
    review goes on without it.
    """

    def __init__(
        self,
        settings: Settings,
        registry: PromptRegistry,
        log: ActivityLog,
        backend_factory: BackendFactory,
    ):
        """Keep the run's settings, prompt registry, activity log and backend factory.

        Args:
            settings: Run settings; ``llm.parallel_subagents`` sets how many subagents
                run at once.
            registry: Approved prompts for the planner and the subagents.
            log: Activity log that receives the review events.
            backend_factory: Creates the model backend for a role (``planner`` or
                ``reviewer``) and, for a reviewer, the subagent's focus.
        """
        self.settings = settings
        self.registry = registry
        self.log = log
        self.factory = backend_factory
        self.prompts_used: set[str] = set()
        self.usage: dict[str, int] = {}
        self.llm_calls = 0
        self.errors: list[
            str
        ] = []  # agents that failed; the report is marked incomplete
        self._lock = threading.Lock()

    def _account(self, usage: dict[str, int], turns: int) -> None:
        with self._lock:  # called from the subagent threads
            self.llm_calls += turns
            for k, v in usage.items():
                self.usage[k] = self.usage.get(k, 0) + v

    def _plan_message(self, ctx: ToolContext) -> str:
        return change_overview(ctx, inline_skills(ctx.skills, "planner")) + (
            "Produce the plan as JSON: focus_files (paths most worth scrutiny), "
            "hypotheses (specific things that might be wrong), notes (one paragraph "
            "for the subagents)."
        )

    def plan(self, ctx: ToolContext) -> ReviewPlan:
        """Ask the planner for focus files, hypotheses and notes for the subagents.

        The planner gets no tools and two turns. When it fails, the error is recorded
        and the plan says to review everything.
        """
        system = self.registry.load("review_planner")
        self.prompts_used.add("review_planner")
        loop = ToolLoop(
            self.factory("planner", None),
            [],
            ctx.safety,
            self.log,
            agent="review_agent",
            max_turns=2,
            max_tokens=4000,
        )
        if (
            ctx.memory is not None and ctx.memory.notes().strip()
        ):  # _plan_message includes up to 3000 chars
            self.log.log(
                "memory_used",
                "review_agent",
                kind="project",
                notes_chars=min(len(ctx.memory.notes()), 3000),
                rejected=[],
                read_from=ctx.memory.read_from,
            )
        res = loop.run(system, self._plan_message(ctx), PLAN_SCHEMA)
        self._account(res.usage, res.turns)
        if not res.ok or not res.data:
            self.log.log("plan_failed", "review_agent", error=res.error)
            self.errors.append(f"planner: {res.error or 'no plan returned'}")
            return with_round_focus(
                ReviewPlan(
                    notes=f"planning failed ({res.error}); reviewing everything"
                ),
                ctx,
            )
        data = res.data
        plan = ReviewPlan(
            focus_files=[str(p) for p in data.get("focus_files", [])][:20],
            hypotheses=[str(h) for h in data.get("hypotheses", [])][:20],
            notes=str(data.get("notes", "")),
        )
        return with_round_focus(plan, ctx)

    def run(self, ctx: ToolContext) -> tuple[ReviewPlan, list[Finding]]:
        """Plan, run the subagents in parallel and return the ranked candidates.

        Reports of the same spot are merged, scanner hits are absorbed, findings the
        project memory says to ignore are dropped, and the rest are grounded and sorted
        by ``rank_key``.

        Returns:
            The plan and the candidate findings in validation order.
        """
        plan = self.plan(ctx)
        self.log.log("plan", "review_agent", plan=plan.model_dump())

        def _one(spec: Any) -> list[Finding]:
            self.prompts_used.add(spec.prompt)
            try:
                backend = self.factory("reviewer", spec.focus)
                findings, res = run_subagent(
                    spec, ctx, backend, self.registry, self.log, plan
                )
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                self.log.log(
                    "subagent_error",
                    f"subagent:{spec.name}",
                    error=error,
                    traceback=traceback.format_exc()[-2000:],
                )
                with self._lock:
                    self.errors.append(f"subagent {spec.name} crashed: {error}")
                return []
            self._account(res.usage, res.turns)
            if res.error:
                with self._lock:
                    self.errors.append(f"subagent {spec.name} failed: {res.error}")
            return findings

        with ThreadPoolExecutor(
            max_workers=max(1, self.settings.llm.parallel_subagents)
        ) as pool:
            batches = list(pool.map(_one, SUBAGENTS))
        raw = [f for batch in batches for f in batch]
        merged = merge_findings(raw)
        findings = absorb_scanner_hits(merged, ctx.scanner_report.findings)
        promoted = len(findings) - len(
            merged
        )  # unreported critical/high scanner hits that became candidates
        ignored = 0
        if ctx.memory is not None and ctx.memory.exists:
            kept = [
                f
                for f in findings
                if not ctx.memory.is_ignored(f.location.path, f.scanner_rule)
            ]
            ignored = len(findings) - len(kept)
            if len(kept) != len(findings):
                self.log.log(
                    "memory_ignored",
                    "review_agent",
                    count=len(findings) - len(kept),
                    stage="review",
                    ids=[f.id for f in findings if f not in kept],
                )
            findings = kept
        ground_findings(findings, ctx)
        findings.sort(key=rank_key)
        self.log.log(
            "candidates",
            "review_agent",
            count=len(findings),
            ids=[f.id for f in findings],
            raw=len(raw),
            grounded=sum(f.grounded for f in findings),
            agreed=sum(f.agreement > 1 for f in findings),
            duplicates=len(raw) - len(merged),
            promoted=promoted,
            ignored=ignored,
            by_agent={b[0].source: len(b) for b in batches if b},
        )
        return plan, findings

    def stats(self) -> dict[str, Any]:
        """Return the review's LLM call count, token usage and the prompts it used."""
        return {
            "review_llm_calls": self.llm_calls,
            "review_usage": dict(self.usage),
            "prompts_used": sorted(self.prompts_used),
        }


def dumps(obj: Any) -> str:
    """Serialise ``obj`` as one-line JSON, writing ``str(value)`` for other types."""
    return json.dumps(obj, default=str)
