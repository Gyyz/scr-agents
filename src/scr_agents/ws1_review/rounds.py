"""Review rounds: settle a round's findings and prepare the next round.

Each round reviews the change (the Review Report: the merged findings of the bug,
security and style reviewers), then the validator checks every finding (the Validator
Report: its verdict, evidence and comments on each one). A finding is settled when the
person reviewing agrees with the validator:

- validator confirmed, human accepted: a real problem, carried into later rounds;
- validator rejected, human rejected: a false positive, kept in the final report as
  rejected but not reviewed again;
- validator unverified, human accepted: a real problem, carried.

Everything else (no human verdict yet, a human verdict that contradicts the validator,
an unverified finding the human rejected) is pending. When the person asks for another
round, it starts again from the planner with the pending findings and both reports as
context, looks at those findings and their surroundings again, and its new Review Report
(``Review Report (round 2)``, then round 3, ...) replaces them. In an automatic run
(``--auto-rounds``) there is no person to ask: the validator's confirmed and rejected
verdicts settle a finding and only unverified ones are reviewed again.
``review.max_rounds`` caps the rounds in total.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scr_agents.models import (
    EvidenceReport,
    Finding,
    ReviewRequest,
    RoundStatus,
    ValidatedFinding,
    Verdict,
    new_id,
    now_iso,
)
from scr_agents.ws4_guardrails.safety_filter import SafetyFilter
from scr_agents.ws5_knowledge.human_review import load_verdicts
from scr_agents.ws5_knowledge.project_memory import finding_fingerprint

REAL = "real"  # settled: a real problem
FALSE = "false"  # settled: a false positive
PENDING = "pending"  # reviewed again if another round starts
NEAR_LINES = 3  # a report this close to a settled finding of the same kind repeats it


class RoundError(ValueError):
    """Another round cannot start: the last one passed or the round limit is reached."""


def labels(round_no: int) -> tuple[str, str]:
    """Return the names of a round's review and validation reports.

    Round 1 writes the Review Report and the Validator Report; later rounds add the
    round, as in ``Review Report (round 2)``.
    """
    if round_no <= 1:
        return "Review Report", "Validator Report"
    return f"Review Report (round {round_no})", f"Validator Report (round {round_no})"


def latest_verdicts(verdicts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return the last human verdict record for each finding id."""
    latest: dict[str, dict[str, Any]] = {}
    for v in verdicts:
        latest[str(v.get("finding_id"))] = v
    return latest


def settle(vf: ValidatedFinding, human: dict[str, Any] | None, auto: bool) -> str:
    """Return whether a finding is settled as ``real``, settled as ``false`` or pending.

    Args:
        vf: The validated finding.
        human: The last human verdict on it in its run (``verdict`` is ``accept`` or
            ``reject``), if any. A finding carried from an earlier round brings its
            own in ``vf.human_verdict``.
        auto: Settle by the validator alone (no person to ask).
    """
    if auto:
        if vf.verdict == Verdict.CONFIRMED:
            return REAL
        return FALSE if vf.verdict == Verdict.REJECTED else PENDING
    said = (human or {}).get("verdict") or vf.human_verdict
    if said == "accept" and vf.verdict in (Verdict.CONFIRMED, Verdict.UNVERIFIED):
        return REAL
    if said == "reject" and vf.verdict == Verdict.REJECTED:
        return FALSE
    return PENDING


def carried_state(vf: ValidatedFinding) -> str:
    """Return how a finding carried from an earlier round was settled: real or false."""
    if vf.verdict == Verdict.CONFIRMED or vf.human_verdict == "accept":
        return REAL
    return FALSE


def round_status(
    report: EvidenceReport,
    verdicts: list[dict[str, Any]],
    max_rounds: int | None = None,
) -> RoundStatus:
    """Classify every finding of a round's report and say whether to offer another.

    Args:
        report: The round's report; findings carried from earlier rounds are settled.
        verdicts: The human verdicts recorded for this run (``load_verdicts``).
        max_rounds: The round limit to apply; ``None`` keeps the one the run had.
    """
    human = latest_verdicts(verdicts)
    limit = max_rounds or report.max_rounds
    label_a, label_b = labels(report.round)
    st = RoundStatus(
        run_id=report.run_id,
        round=report.round,
        max_rounds=limit,
        label_a=label_a,
        label_b=label_b,
        auto=report.auto_rounds,
    )
    for vf in report.findings:
        fid = vf.finding.id
        if vf.carried_from is None and vf.verdict != Verdict.CONFIRMED:
            st.validator_passed = False
        state = (
            carried_state(vf)
            if vf.carried_from is not None
            else settle(vf, human.get(fid), report.auto_rounds)
        )
        if state == REAL:
            st.settled_real.append(fid)
        elif state == FALSE:
            st.settled_false.append(fid)
        else:
            st.pending.append(fid)
            if not report.auto_rounds:
                (st.disputed if fid in human else st.awaiting_verdict).append(fid)
    st.passed = not st.pending
    st.can_continue = bool(st.pending) and report.round < limit
    st.ask_user = (
        not report.auto_rounds
        and st.can_continue
        and (not st.validator_passed or bool(st.disputed))
    )
    return st


def run_status(run_dir: Path, max_rounds: int | None = None) -> RoundStatus:
    """Return the round status of the run in ``run_dir`` (its ``report.json``)."""
    report = EvidenceReport.model_validate_json((run_dir / "report.json").read_text())
    return round_status(report, load_verdicts(run_dir), max_rounds)


@dataclass
class PriorRound:
    """What the next round knows about the round before it.

    ``settled`` are copies of that round's settled findings, marked as carried and with
    the human verdict that settled them; ``pending`` are the findings to review again.
    """

    round: int  # the round about to run
    max_rounds: int
    previous: EvidenceReport
    previous_dir: Path
    status: RoundStatus
    settled: list[ValidatedFinding] = field(default_factory=list)
    pending: list[ValidatedFinding] = field(default_factory=list)
    human: dict[str, dict[str, Any]] = field(default_factory=dict)


def prepare_round(previous_dir: Path, max_rounds: int) -> PriorRound:
    """Read a finished round and work out what the next round carries and reviews.

    Raises:
        RoundError: If the round passed (nothing is pending) or ``max_rounds`` rounds
            have already run.
        FileNotFoundError: If ``previous_dir`` has no ``report.json``.
    """
    previous = EvidenceReport.model_validate_json(
        (previous_dir / "report.json").read_text()
    )
    verdicts = load_verdicts(previous_dir)
    status = round_status(previous, verdicts, max_rounds)
    if status.passed:
        raise RoundError(
            f"round {previous.round} ({previous.run_id}) passed: every finding is "
            "settled, so there is nothing to review again"
        )
    if previous.round >= max_rounds:
        raise RoundError(
            f"the round limit is reached ({previous.round} of {max_rounds}); raise "
            "review.max_rounds in the configuration to review again"
        )
    human = latest_verdicts(verdicts)
    prior = PriorRound(
        round=previous.round + 1,
        max_rounds=max_rounds,
        previous=previous,
        previous_dir=previous_dir,
        status=status,
        human=human,
    )
    pending = set(status.pending)
    for vf in previous.findings:
        if vf.finding.id in pending:
            prior.pending.append(vf)
            continue
        carried = vf.model_copy(deep=True)
        if carried.carried_from is None:
            carried.carried_from = previous.run_id
            h = human.get(vf.finding.id)
            if h is not None:
                carried.human_verdict = str(h.get("verdict"))
                carried.human_note = str(h.get("note") or "")
        prior.settled.append(carried)
    return prior


def next_request(previous: EvidenceReport) -> ReviewRequest:
    """Return the next round's request: the change of ``previous``, a new run id."""
    req = previous.request.model_copy(deep=True)
    req.run_id = new_id("run")
    req.created_at = now_iso()
    return req


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def round_section(prior: PriorRound | None, safety: SafetyFilter) -> str:
    """Write the part of the planner's and subagents' task that earlier rounds give.

    It lists the findings to review again with the validator's comments (the Validator
    Report) and
    the human verdicts, and the settled findings not to report again. Their text comes
    from the repository through the agents, so it is wrapped by the safety filter.
    """
    if prior is None:
        return ""
    prev_a, prev_b = labels(prior.previous.round)
    new_a, _ = labels(prior.round)
    rows = []
    for vf in prior.pending:
        f = vf.finding
        h = prior.human.get(f.id)
        human = (
            f"; the person reviewing said {h.get('verdict')}"
            + (f" ({_clip(str(h.get('note') or ''), 300)})" if h.get("note") else "")
            if h
            else "; the person reviewing gave no verdict"
            if not prior.previous.auto_rounds
            else ""
        )
        rows.append(
            f"- {f.id} [{f.category.value}, {f.severity.value}] "
            f"{f.location.path}:{f.location.line or '?'} {f.title}\n"
            f"  claim: {_clip(f.description, 500)}\n"
            f"  validator: {vf.verdict.value} (evidence {vf.evidence_strength}): "
            f"{_clip(vf.validator_notes, 600) or 'no comment'}{human}"
        )
    settled = [
        f"- {vf.finding.location.path}:{vf.finding.location.line or '?'} "
        f"{vf.finding.title} "
        f"({'real' if carried_state(vf) == REAL else 'false positive'})"
        for vf in prior.settled
    ]
    listing = (
        "Findings to review again:\n"
        + "\n".join(rows)
        + (
            "\n\nSettled findings (already decided; do not report them again):\n"
            + "\n".join(settled)
            if settled
            else ""
        )
    )
    return (
        f"\nReview round {prior.round} of {prior.max_rounds}. In round "
        f"{prior.previous.round} the reviewers wrote the {prev_a} and the validator "
        f"commented on every finding in the {prev_b}. The findings below were "
        "rejected, not verified or disputed. Look at each one again: report it once "
        "more with corrected location, description and evidence if the code shows it "
        "is real, or leave it out if it is not. Also look for new problems in the same "
        "files and functions near these spots. Your findings form the "
        f"{new_a}.\n" + safety.wrap(listing, "previous_round", 12000) + "\n"
    )


def drop_settled(
    candidates: list[Finding], settled: list[ValidatedFinding]
) -> tuple[list[Finding], list[str]]:
    """Leave out new candidates that repeat a settled finding.

    A candidate repeats one when it has the same fingerprint, or the same category in
    the same file within ``NEAR_LINES`` lines.

    Returns:
        The candidates to validate and the ids of those left out.
    """
    prints = {finding_fingerprint(vf.finding) for vf in settled}
    spots = [
        (vf.finding.location.path, vf.finding.category, vf.finding.location.line)
        for vf in settled
    ]
    kept: list[Finding] = []
    dropped: list[str] = []
    for f in candidates:
        line = f.location.line
        repeat = finding_fingerprint(f) in prints or any(
            path == f.location.path
            and cat == f.category
            and line is not None
            and other is not None
            and abs(line - other) <= NEAR_LINES
            for path, cat, other in spots
        )
        if repeat:
            dropped.append(f.id)
        else:
            kept.append(f)
    return kept, dropped
