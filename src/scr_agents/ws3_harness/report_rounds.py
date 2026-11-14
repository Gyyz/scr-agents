"""The two HTML pages of a review round: the Review Report and the Validator Report.

Report A (``report_a.html``; A1, A2, ... in later rounds) holds the findings the
reviewers reported in this round, each with the validator's comment under it. Report B
(``report_b.html``; B1, B2, ...) is the validator's comment report on A: its verdict,
evidence and comment on every finding, the human verdicts so far, which findings are
settled or pending, and whether to offer another round. ``report.html`` stays the
combined report, with the findings carried from earlier rounds.
"""

from __future__ import annotations

import secrets
from collections import Counter
from pathlib import Path
from typing import Any

from scr_agents.models import EvidenceReport, RoundStatus, Verdict
from scr_agents.ws1_review.rounds import (
    FALSE,
    PENDING,
    REAL,
    labels,
    latest_verdicts,
    round_status,
)
from scr_agents.ws3_harness.report_html import (
    _STRENGTH,
    CSS,
    _repo_name,
    clip,
    csp,
    e,
    prose,
    render_html,
)

REPORT_A, REPORT_B = "report_a.html", "report_b.html"
_STATE_LABEL = {REAL: "settled: real", FALSE: "settled: false positive"}


def round_findings(report: EvidenceReport) -> EvidenceReport:
    """Return a copy of ``report`` with only the findings reported in its own round."""
    return report.model_copy(
        update={"findings": [v for v in report.findings if v.carried_from is None]}
    )


def render_report_a(
    report: EvidenceReport,
    human_verdicts: list[dict[str, Any]] | None = None,
    nonce: str | None = None,
) -> str:
    """Render the Review Report, with the validator's comment on each finding."""
    label_a, label_b = labels(report.round)
    return render_html(
        round_findings(report),
        human_verdicts,
        nonce=nonce,
        label=f"{label_a} · validator comments from the {label_b}",
    )


def _decision(st: RoundStatus) -> tuple[str, str]:
    """Return the banner class and text that say where the round stands."""
    nxt = st.round + 1
    if st.passed:
        return "ok", (
            "<b>Passed.</b> Every finding is settled"
            + (" by the validator." if st.auto else ": the validator and you agree.")
        )
    if not st.can_continue:
        return "warn", (
            f"<b>Round limit reached ({st.round} of {st.max_rounds}).</b> "
            f"{len(st.pending)} finding(s) stay unsettled; raise "
            "<code>review.max_rounds</code> to review them again."
        )
    if st.auto:
        return "warn", (
            f"<b>{len(st.pending)} finding(s) not verified.</b> An automatic run "
            f"reviews them again in round {nxt}."
        )
    if st.ask_user:
        return "warn", (
            f"<b>The validator does not agree with every finding of the "
            f"{e(st.label_a)}.</b> {len(st.pending)} finding(s) are pending "
            f"({len(st.disputed)} disputed, {len(st.awaiting_verdict)} without your "
            "verdict). Accept or reject them, then decide whether to start round "
            f"{nxt}: it reviews the pending findings again from the planner, with "
            f"the {e(st.label_a)} and the {e(st.label_b)} as context."
        )
    return "note", (
        "<b>The validator confirmed every finding.</b> Accept or reject them to "
        f"settle the review; a rejected one can be reviewed again in round {nxt}."
    )


def render_report_b(
    report: EvidenceReport,
    human_verdicts: list[dict[str, Any]] | None = None,
    nonce: str | None = None,
    max_rounds: int | None = None,
) -> str:
    """Render the Validator Report: the validator's comments on the Review Report.

    Args:
        report: The round's report.
        human_verdicts: Human verdicts recorded for the run so far.
        nonce: The script nonce, when the response also sends ``csp(nonce)``.
        max_rounds: The round limit to apply; ``None`` keeps the run's own.
    """
    verdicts = human_verdicts or []
    st = round_status(report, verdicts, max_rounds)
    human = latest_verdicts(verdicts)
    own = round_findings(report).findings
    carried = [v for v in report.findings if v.carried_from]
    state = {
        **dict.fromkeys(st.settled_real, REAL),
        **dict.fromkeys(st.settled_false, FALSE),
        **dict.fromkeys(st.pending, PENDING),
    }
    counts = Counter(v.verdict.value for v in own)
    strengths = Counter(v.evidence_strength for v in own)
    repo = _repo_name(report)
    nonce = nonce or secrets.token_urlsafe(12)
    kind, decision = _decision(st)

    def human_cell(fid: str, carried_verdict: str | None) -> str:
        h = human.get(fid)
        said = h.get("verdict") if h else carried_verdict
        if not said:
            return '<span class="muted">none yet</span>'
        note = (h or {}).get("note")
        return f'<span class="tag human {e(said)}">{e(said)}</span>' + (
            f" <span class=small>{e(clip(note, 200))}</span>" if note else ""
        )

    rows = "".join(
        f'<tr><td><a href="{REPORT_A}#{e(v.finding.id)}">{e(v.finding.id)}</a></td>'
        f"<td>{e(v.finding.title)}<div class='muted small'>"
        f"{e(v.finding.location.path)}:{e(v.finding.location.line or '?')} · "
        f"{e(v.finding.category.value)}</div></td>"
        f'<td class="sev {e(v.finding.severity.value)}">'
        f"{e(v.finding.severity.value)}</td>"
        f'<td><span class="verdict {e(v.verdict.value)}">{e(v.verdict.value)}</span>'
        "<div class='muted small' "
        f'title="{e(_STRENGTH.get(v.evidence_strength, ""))}">'
        f"evidence: {e(v.evidence_strength)}</div></td>"
        f"<td>{prose(clip(v.validator_notes, 900)) or '<span class=muted>none</span>'}"
        "</td>"
        f"<td>{human_cell(v.finding.id, None)}</td>"
        f'<td><span class="state {state.get(v.finding.id, PENDING)}">'
        f"{e(_STATE_LABEL.get(state.get(v.finding.id, PENDING), 'pending'))}"
        "</span></td></tr>"
        for v in sorted(
            own,
            key=lambda v: (
                state.get(v.finding.id) != PENDING,
                v.verdict == Verdict.CONFIRMED,
            ),
        )
    )
    carried_rows = "".join(
        f"<tr><td>{e(v.finding.id)}</td><td>{e(v.finding.title)}"
        f"<div class='muted small'>{e(v.finding.location.path)}:"
        f"{e(v.finding.location.line or '?')}</div></td>"
        f"<td>round {v.round}</td>"
        f'<td><span class="verdict {e(v.verdict.value)}">{e(v.verdict.value)}</span>'
        f"</td><td>{human_cell(v.finding.id, v.human_verdict)}</td></tr>"
        for v in carried
    )
    tally = (
        '<div class="tally">'
        + "".join(
            f'<div class="tile {k}"><div class="n">{counts[k]}</div>'
            f'<div class="l">{k}</div></div>'
            for k in ("confirmed", "unverified", "rejected")
        )
        + '<div class="tile"><div class="l">settled / pending</div>'
        f'<p class="small">{len(st.settled_real)} real · {len(st.settled_false)} '
        f"false positive · {len(st.pending)} pending</p>"
        f'<p class="muted small">Evidence: '
        + (", ".join(f"{n} {e(k)}" for k, n in strengths.most_common()) or "none")
        + "</p></div></div>"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{
        csp(nonce).replace("; frame-ancestors 'none'", "")
    }">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex, nofollow">
<title>{e(repo)} · {e(st.label_b)}</title>
<style>{CSS}</style>
</head>
<body>
<header class="hero"><div class="wrap">
  <div class="brand">SCR Agents · {e(st.label_b)}</div>
  <h1>{e(repo)}</h1>
  <div class="sub">The validator's comments on the {e(st.label_a)} ·
    round {st.round} of {st.max_rounds}</div>
  <div class="meta"><span>run {e(report.run_id)}</span>
    <span>{len(own)} finding(s) checked</span>
    {f"<span>{len(carried)} carried from earlier rounds</span>" if carried else ""}
    {
        f"<span>previous round: run {e(report.previous_run_id)}</span>"
        if report.previous_run_id
        else ""
    }</div>
  {tally}
  <div class="banner {kind}">{decision}</div>
</div></header>
<main class="wrap">
<section id="comments"><h2>Comments on the {e(st.label_a)}</h2>
  {
        "<table><tr><th>finding</th><th>claim</th><th>severity</th><th>verdict</th>"
        "<th>validator comment</th><th>your verdict</th><th>state</th></tr>"
        f"{rows}</table>"
        if rows
        else '<div class="empty">The reviewers reported no findings this round.</div>'
    }
  <p class="muted small">Pending findings are reviewed again if another round
    starts. Open the <a href="{REPORT_A}">{e(st.label_a)}</a> for each
    finding's evidence and fix.</p></section>
{
        "<section id='carried'><h2>Settled in earlier rounds</h2><table><tr>"
        "<th>finding</th><th>claim</th><th>found</th><th>verdict</th>"
        f"<th>human verdict</th></tr>{carried_rows}</table></section>"
        if carried_rows
        else ""
    }
<footer>Generated by SCR Agents from {e(report.run_id)}.</footer>
</main>
</body>
</html>
"""


def write_round_reports(report: EvidenceReport, out_dir: Path) -> tuple[Path, Path]:
    """Write ``report_a.html`` and ``report_b.html`` of a round into ``out_dir``.

    Returns:
        The paths of the Review Report and the Validator Report.
    """
    a, b = out_dir / REPORT_A, out_dir / REPORT_B
    a.write_text(render_report_a(report), encoding="utf-8")
    b.write_text(render_report_b(report), encoding="utf-8")
    return a, b
