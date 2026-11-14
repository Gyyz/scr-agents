"""Human review packets and recorded verdicts.

A packet for a reviewer to check early versions, and recorded verdicts that feed back
into the approved registry's A/B statistics.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scr_agents.models import EvidenceReport, now_iso
from scr_agents.ws4_guardrails.registry import PromptRegistry
from scr_agents.ws5_knowledge.project_memory import ProjectMemory


def write_packet(report: EvidenceReport, out_dir: Path) -> Path:
    """Write ``human_review.md``: a Markdown checklist of the run's findings.

    The packet also shows the ``scr-agents verdict`` command that records a
    reviewer's decision on a finding.

    Args:
        report: The finished run whose findings the reviewer checks.
        out_dir: The run directory; the packet is written there.

    Returns:
        The path of the written packet.
    """
    lines = [
        f"# Human review packet: {report.run_id}",
        "",
        f"Repo: {report.request.repo_path}",
        (
            f"Range: {report.request.base[:12]}..{report.request.head[:12]}"
            f"  task={report.request.task}"
        ),
        "",
        "Tick each finding you agree with, then record verdicts with:",
        "",
        (
            f"    scr-agents verdict --run-dir {out_dir} --finding <id>"
            " --verdict accept|reject --note '...'"
        ),
        "",
        "| | id | verdict | severity | title | location |",
        "|-|-|-|-|-|-|",
    ]
    for vf in report.findings:
        f = vf.finding
        loc = f"{f.location.path}:{f.location.line or '?'}"
        lines.append(
            f"| [ ] | {f.id} | {vf.verdict} | {f.severity} | {f.title} | {loc} |"
        )
    path = out_dir / "human_review.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def record_verdict(
    out_dir: Path,
    finding_id: str,
    verdict: str,
    note: str = "",
    registry: PromptRegistry | None = None,
) -> dict[str, Any]:
    """Record a reviewer's verdict on one finding of a finished run.

    The verdict is appended to ``human_verdicts.jsonl`` in the run directory,
    credited in ``registry`` to the prompts behind the finding (the validator
    and, for a subagent finding, that subagent's prompt), and written into the
    finding's record in the project memory's findings history, if it has one.

    Args:
        out_dir: The run directory holding ``report.json``.
        finding_id: Id of the finding, as shown in the report.
        verdict: ``"accept"`` or ``"reject"``.
        note: Free-text reason kept with the verdict.
        registry: Prompt registry whose A/B statistics get the outcome; ``None``
            records the verdict without crediting any prompt.

    Returns:
        The record written, plus ``memory_updated``: whether the project
        memory's findings history was updated.

    Raises:
        ValueError: If ``verdict`` is neither ``"accept"`` nor ``"reject"``.
        KeyError: If the report has no finding with ``finding_id``.
    """
    if verdict not in ("accept", "reject"):
        raise ValueError("verdict must be accept or reject")
    report_path = out_dir / "report.json"
    report = EvidenceReport.model_validate_json(report_path.read_text())
    match = next((vf for vf in report.findings if vf.finding.id == finding_id), None)
    if match is None:
        raise KeyError(f"finding {finding_id} not in {report_path}")
    rec: dict[str, Any] = {
        "ts": now_iso(),
        "run_id": report.run_id,
        "finding_id": finding_id,
        "verdict": verdict,
        "note": note,
        "agent_verdict": match.verdict,
        "source": match.finding.source,
    }
    with (out_dir / "human_verdicts.jsonl").open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    if registry is not None:
        # credit the prompt that produced the finding and the validator that judged it,
        # nothing else
        credited = {"validator"}
        source = match.finding.source
        if source.startswith("subagent:"):
            credited.add("subagent_" + source.split(":", 1)[1])
        for prompt in sorted(credited):
            registry.record_outcome(
                prompt, verdict == "accept", report.run_id, finding_id
            )
    try:
        rec["memory_updated"] = ProjectMemory(
            report.request.repo_path
        ).record_human_verdict(report.run_id, finding_id, verdict, note)
    except OSError:
        rec["memory_updated"] = False
    return rec


def load_verdicts(out_dir: Path) -> list[dict[str, Any]]:
    """Return the verdicts recorded for a run, oldest first, or ``[]`` if none."""
    p = out_dir / "human_verdicts.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
