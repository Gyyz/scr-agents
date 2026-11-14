"""OpenFlow: an LLM orchestrates a review round instead of fixed code.

The orchestrator model gets a brief of the change (``change_overview``: files, blast
radius, scanner counts, project memory, the round before, commits, the diff) and tools:

- the read-only review tools, its skills and the MCP servers assigned to
  ``orchestrator``;
- ``run_reviewer``: run the bugs, vulnerabilities or style subagent on the files and
  hypotheses it chooses; the findings join the round's candidate pool;
- ``list_candidates``: the pool, merged and grounded, with the verdicts so far;
- ``validate``: send candidates to the validator (the second model, in the sandbox).

It ends with a JSON answer: the candidate ids to report, the ones it drops (each with
a reason) and notes for the report.

Code keeps the guardrails of the deterministic flow. Reports of the same spot merge
into one candidate, findings the project memory ignores and repeats of settled
findings are left out, every candidate is grounded against the head checkout, the
validator budget (``llm.max_findings_to_validate`` and the style limit) holds for the
whole round and the orchestrator cannot change a verdict. At the end, scanner hits are
absorbed as in the deterministic flow, a confirmed finding is always reported, a
candidate neither reported nor dropped is reported anyway, and a reported candidate
nobody validated is validated while the budget lasts. When the orchestrator fails, the
round degrades to the deterministic flow for whatever has not run yet.
"""

from __future__ import annotations

import threading
import traceback
from typing import Any

from scr_agents.config import Settings
from scr_agents.llm.backend import ToolSpec
from scr_agents.llm.loop import ToolLoop
from scr_agents.models import Finding, ReviewPlan, ValidatedFinding, Verdict
from scr_agents.ws1_review.orchestrator import (
    BackendFactory,
    ReviewAgent,
    absorb_scanner_hits,
    change_overview,
    ground_findings,
    merge_findings,
    rank_key,
)
from scr_agents.ws1_review.rounds import drop_settled
from scr_agents.ws1_review.subagents import SUBAGENTS, run_subagent
from scr_agents.ws1_review.tools import ToolContext, review_tools
from scr_agents.ws1_review.validator import LIGHT_CATEGORIES, ValidatorAgent
from scr_agents.ws4_guardrails.registry import PromptRegistry
from scr_agents.ws5_knowledge.activity_log import ActivityLog

AGENT = "orchestrator"  # activity log agent and component (skills, MCP servers)
PROMPT = "openflow_orchestrator"
MAX_REVIEWER_RUNS = 9  # run_reviewer calls per round, all three reviewers together
NOTES_CHARS = 600  # validator notes shown to the orchestrator per candidate

FINAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "report": {"type": "array", "items": {"type": "string"}},
        "dropped": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "reason"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["report", "dropped", "notes"],
    "additionalProperties": False,
}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


class OpenFlowAgent:
    """Let the orchestrator model run one review round through its tools.

    ``run`` returns the round's plan (what the reviewers were asked to look at) and the
    validated findings to report, like ``ReviewAgent.run`` followed by
    ``ValidatorAgent.validate`` in the deterministic flow. LLM calls, token usage, the
    prompts used and agent failures are collected for the run report.
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
            settings: Run settings; ``review.openflow_max_turns`` caps the
                orchestrator's turns and the ``llm`` section the validation budget.
            registry: Approved prompts for the orchestrator, subagents and validator.
            log: Activity log that receives the round's events.
            backend_factory: Creates the model backend for a role: ``orchestrator``,
                ``reviewer`` (with the subagent's focus) or ``validator``.
        """
        self.settings = settings
        self.registry = registry
        self.log = log
        self.factory = backend_factory
        # the reviewers' and the validators' own accounting, reused as in the
        # deterministic flow
        self.review = ReviewAgent(settings, registry, log, backend_factory)
        self.validator = ValidatorAgent(settings, registry, log, backend_factory)
        self.prompts_used: set[str] = {PROMPT}
        self.errors: list[str] = []
        self.candidates: list[Finding] = []  # the pool, merged, in arrival order
        self.verdicts: dict[str, ValidatedFinding] = {}
        self.reviewer_runs: list[dict[str, Any]] = []
        self.dropped: list[dict[str, str]] = []
        self.left_out: dict[str, str] = {}  # id -> why code left it out of the pool
        self.turns = 0
        self.notes = ""
        self._n_style = 0
        self._lock = threading.Lock()

    # the candidate pool ---------------------------------------------------------
    def _add(self, ctx: ToolContext, found: list[Finding]) -> dict[str, list[str]]:
        """Merge new reports into the pool; returns the new and merged-into ids."""
        new: list[str] = []
        merged: list[str] = []
        if ctx.prior_round is not None:
            found, repeats = drop_settled(found, ctx.prior_round.settled)
            for fid in repeats:
                self.left_out[fid] = "repeats a finding settled in an earlier round"
        if ctx.memory is not None and ctx.memory.exists:
            kept = [
                f
                for f in found
                if not ctx.memory.is_ignored(f.location.path, f.scanner_rule)
            ]
            for f in found:
                if f not in kept:
                    self.left_out[f.id] = "ignored by the project memory"
            found = kept
        ground_findings(found, ctx)
        with self._lock:
            for f in found:
                for i, c in enumerate(self.candidates):
                    out = merge_findings([c, f])
                    if len(out) != 1:
                        continue
                    sources = {*c.merged_from, c.source}
                    m = out[0]
                    m.id = c.id  # ids the orchestrator has seen stay valid
                    m.merged_from = sorted({*m.merged_from, *sources})
                    m.agreement = len(m.merged_from)
                    self.candidates[i] = m
                    merged.append(c.id)
                    break
                else:
                    self.candidates.append(f)
                    new.append(f.id)
        return {"new": new, "merged_into": sorted(set(merged))}

    def _by_id(self, fid: str) -> Finding | None:
        return next((c for c in self.candidates if c.id == fid), None)

    def _validate(self, ctx: ToolContext, ids: list[str]) -> list[ValidatedFinding]:
        """Validate the candidates ``ids`` that are new, within the round's budget."""
        llm = self.settings.llm
        chosen: list[Finding] = []
        with self._lock:
            for fid in dict.fromkeys(ids):
                f = self._by_id(fid)
                if f is None or fid in self.verdicts:
                    continue
                is_style = f.category in LIGHT_CATEGORIES
                if len(self.verdicts) + len(chosen) >= llm.max_findings_to_validate:
                    break
                if is_style and self._n_style >= llm.max_style_findings_to_validate:
                    continue
                self._n_style += is_style
                chosen.append(f)
            for f in chosen:  # reserved: a parallel call does not pick them again
                self.verdicts[f.id] = ValidatedFinding(finding=f)
        results = self.validator.validate(ctx, chosen) if chosen else []
        with self._lock:
            for vf in results:
                self.verdicts[vf.finding.id] = vf
        return results

    # the orchestrator's tools ---------------------------------------------------
    def _tools(self, ctx: ToolContext) -> list[ToolSpec]:
        names = [s.name for s in SUBAGENTS]
        specs = {s.name: s for s in SUBAGENTS}

        def run_reviewer(inp: dict[str, Any]) -> dict[str, Any]:
            spec = specs.get(str(inp.get("agent")))
            if spec is None:
                return {"error": f"unknown reviewer; choose from {names}"}
            with self._lock:
                if len(self.reviewer_runs) >= MAX_REVIEWER_RUNS:
                    return {
                        "error": f"the round's {MAX_REVIEWER_RUNS} reviewer runs are "
                        "used up; validate and finish"
                    }
                entry: dict[str, Any] = {"agent": spec.name}
                self.reviewer_runs.append(entry)
            plan = ReviewPlan(
                focus_files=[str(p) for p in inp.get("files") or []][:20],
                hypotheses=[str(h) for h in inp.get("hypotheses") or []][:20],
                notes=str(inp.get("notes") or ""),
            )
            entry.update(files=plan.focus_files, hypotheses=plan.hypotheses)
            self.prompts_used.add(spec.prompt)
            try:
                found, res = run_subagent(
                    spec,
                    ctx,
                    self.factory("reviewer", spec.focus),
                    self.registry,
                    self.log,
                    plan,
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
                entry["error"] = error
                return {"agent": spec.name, "error": error}
            self.review._account(res.usage, res.turns)
            if res.error:
                with self._lock:
                    self.errors.append(f"subagent {spec.name} failed: {res.error}")
            added = self._add(ctx, found)
            entry.update(reported=len(found), **added)
            return {
                "agent": spec.name,
                "reported": len(found),
                **added,
                "error": res.error,
                "candidates": len(self.candidates),
            }

        def list_candidates(_: dict[str, Any]) -> list[dict[str, Any]]:
            with self._lock:
                pool = sorted(self.candidates, key=rank_key)
                verdicts = dict(self.verdicts)
            out = []
            for f in pool:
                vf = verdicts.get(f.id)
                out.append(
                    {
                        "id": f.id,
                        "title": f.title,
                        "category": f.category.value,
                        "severity": f.severity.value,
                        "location": f"{f.location.path}:{f.location.line or '?'}",
                        "sources": f.merged_from or [f.source],
                        "agreement": f.agreement,
                        "grounded": f.grounded,
                        "confidence": f.confidence,
                        "verdict": vf.verdict.value if vf is not None else None,
                        "evidence": vf.evidence_strength if vf is not None else None,
                        "validator_notes": vf.validator_notes[:NOTES_CHARS]
                        if vf is not None
                        else None,
                    }
                )
            return out

        def validate(inp: dict[str, Any]) -> dict[str, Any]:
            ids = [str(i) for i in inp.get("ids") or []]
            unknown = [i for i in ids if self._by_id(i) is None]
            results = self._validate(ctx, ids)
            with self._lock:
                used = len(self.verdicts)
            return {
                "validated": [
                    {
                        "id": vf.finding.id,
                        "verdict": vf.verdict.value,
                        "evidence": vf.evidence_strength,
                        "notes": vf.validator_notes[:NOTES_CHARS],
                    }
                    for vf in results
                ],
                "unknown_ids": unknown,
                "budget_left": max(
                    0, self.settings.llm.max_findings_to_validate - used
                ),
            }

        return [
            *review_tools(ctx, component=AGENT),
            ToolSpec(
                "run_reviewer",
                "Run one reviewer subagent (bugs, vulnerabilities or style) with its "
                "own tools. Give it the files to focus on, specific hypotheses and "
                "notes. Its findings join the candidate pool; reports of the same spot "
                "merge into one candidate. Returns the new and merged candidate ids.",
                _obj(
                    {
                        "agent": {"type": "string", "enum": names},
                        "files": {"type": "array", "items": {"type": "string"}},
                        "hypotheses": {"type": "array", "items": {"type": "string"}},
                        "notes": {"type": "string"},
                    },
                    ["agent"],
                ),
                run_reviewer,
                untrusted=True,
            ),
            ToolSpec(
                "list_candidates",
                "The candidate pool in validation order: id, title, location, sources, "
                "grounding and, once validated, the verdict and the validator's notes.",
                _obj({}),
                list_candidates,
                untrusted=True,
            ),
            ToolSpec(
                "validate",
                "Send candidates to the validator, an independent model that reads, "
                "builds and runs the code in a sandbox. Its verdict is final. The "
                "round has a limited budget of validations.",
                _obj({"ids": {"type": "array", "items": {"type": "string"}}}, ["ids"]),
                validate,
                untrusted=True,
            ),
        ]

    def _message(self, ctx: ToolContext) -> str:
        llm = self.settings.llm
        return change_overview(ctx) + (
            f"Reviewers you can run: {', '.join(s.name for s in SUBAGENTS)} "
            f"(at most {MAX_REVIEWER_RUNS} runs in total). Validation budget: "
            f"{llm.max_findings_to_validate} findings, at most "
            f"{llm.max_style_findings_to_validate} of them style or naming.\n"
            "Orchestrate the review with your tools, then answer with JSON: report "
            "(candidate ids to report), dropped (ids you leave out, each with a "
            "reason) and notes (one paragraph for the report)."
        )

    # the round ------------------------------------------------------------------
    def _fallback(self, ctx: ToolContext) -> None:
        """Do in code what the orchestrator left undone: review, if nothing ran."""
        if self.reviewer_runs:
            return
        self.log.log("openflow_fallback", AGENT, step="review")
        plan, found = self.review.run(ctx)
        self.errors.extend(self.review.errors)
        self.review.errors = []
        self.notes = self.notes or plan.notes
        self.reviewer_runs.append(
            {
                "agent": "deterministic",
                "files": plan.focus_files,
                "reported": len(found),
            }
        )
        self._add(ctx, found)

    def run(self, ctx: ToolContext) -> tuple[ReviewPlan, list[ValidatedFinding]]:
        """Run the round: the orchestrator's tool loop, then the guardrails in code.

        Returns:
            The plan (the files and hypotheses the reviewers were given, and the
            orchestrator's notes) and the findings to report: the validated ones in
            rank order, then the unverified ones.
        """
        loop = ToolLoop(
            self.factory(AGENT, None),
            self._tools(ctx),
            ctx.safety,
            self.log,
            agent=AGENT,
            max_turns=self.settings.review.openflow_max_turns,
            max_tokens=self.settings.llm.max_tokens,
            serial_lock=ctx.serial_lock,
        )
        self.log.log("openflow_start", AGENT, model=getattr(loop.backend, "model", ""))
        answer: dict[str, Any] = {}
        try:
            res = loop.run(self.registry.load(PROMPT), self._message(ctx), FINAL_SCHEMA)
            self.review._account(res.usage, res.turns)
            self.turns = res.turns
            if res.ok and res.data:
                answer = res.data
            else:
                self.errors.append(f"orchestrator: {res.error or 'no answer'}")
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            self.log.log(
                "openflow_error",
                AGENT,
                error=error,
                traceback=traceback.format_exc()[-2000:],
            )
            self.errors.append(f"orchestrator crashed: {error}")
        if not answer:
            self._fallback(ctx)
        self.notes = str(answer.get("notes") or self.notes)

        # scanner hits strengthen candidates; unreported critical/high ones join
        with self._lock:
            known = {c.id for c in self.candidates}
            self.candidates = absorb_scanner_hits(
                self.candidates, ctx.scanner_report.findings
            )
            promoted = [c for c in self.candidates if c.id not in known]
        ground_findings(promoted, ctx)

        reported = [str(i) for i in answer.get("report") or []]
        reasons = {
            str(d.get("id")): str(d.get("reason") or "")
            for d in answer.get("dropped") or []
            if isinstance(d, dict)
        }
        keep: list[Finding] = []
        for c in sorted(self.candidates, key=rank_key):
            vf = self.verdicts.get(c.id)
            confirmed = vf is not None and vf.verdict == Verdict.CONFIRMED
            if c.id in reasons and c.id not in reported and not confirmed:
                self.dropped.append(
                    {"id": c.id, "title": c.title, "reason": reasons[c.id]}
                )
            else:
                keep.append(c)
        self._validate(ctx, [c.id for c in keep])
        if self.dropped:
            self.log.log("openflow_dropped", AGENT, findings=self.dropped)
        self.errors.extend(self.validator.errors)

        validated = [self.verdicts[c.id] for c in keep if c.id in self.verdicts]
        checked = {vf.finding.id for vf in validated}
        rest = [
            ValidatedFinding(
                finding=c,
                verdict=Verdict.UNVERIFIED,
                validator_notes="not validated: the round's validation budget was "
                "used up",
            )
            for c in keep
            if c.id not in checked
        ]
        runs = [r for r in self.reviewer_runs if r.get("agent") != "deterministic"]
        plan = ReviewPlan(
            focus_files=list(
                dict.fromkeys(p for r in self.reviewer_runs for p in r.get("files", []))
            )[:20],
            hypotheses=list(
                dict.fromkeys(h for r in runs for h in r.get("hypotheses", []))
            )[:20],
            notes=self.notes,
        )
        self.log.log(
            "openflow_done",
            AGENT,
            turns=self.turns,
            reviewer_runs=len(self.reviewer_runs),
            candidates=len(self.candidates),
            validated=len(checked),
            reported=len(validated) + len(rest),
            dropped=len(self.dropped),
            promoted=len(promoted),
            left_out=len(self.left_out),
        )
        return plan, [*validated, *rest]

    def stats(self) -> dict[str, Any]:
        """Return the round's LLM calls and usage, and what the orchestrator did."""
        return {
            **self.review.stats(),
            **self.validator.stats(),
            "prompts_used": sorted(
                self.prompts_used
                | self.review.prompts_used
                | self.validator.prompts_used
            ),
            "openflow": {
                "turns": self.turns,
                "reviewer_runs": self.reviewer_runs,
                "dropped": self.dropped,
                "left_out": self.left_out,
            },
        }
