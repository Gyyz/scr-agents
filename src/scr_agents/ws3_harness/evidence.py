"""Evidence report: Markdown, JSON and HTML renderings of the validated findings.

The HTML rendering is in report_html.py.
"""

from __future__ import annotations

from pathlib import Path

from scr_agents.models import EvidenceReport, Verdict

_ORDER = {Verdict.CONFIRMED: 0, Verdict.UNVERIFIED: 1, Verdict.REJECTED: 2}
_SEV = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _ref(sha: str, ref: str) -> str:
    return "" if sha.startswith(ref) or ref.startswith(sha) else f" ({ref})"


def _clip(text: str | None, n: int = 1500) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + f"\n... [{len(text) - n} more chars]"


def render_markdown(report: EvidenceReport) -> str:
    """Render the report as Markdown, confirmed findings first, then by severity."""
    r = report.request
    counts = report.counts()
    lines = [
        f"# Code review evidence report: {report.run_id}",
        "",
        f"- Repo: `{r.repo_path}`",
        f"- Range: `{r.base[:12]}`{_ref(r.base, r.base_ref)} -> "
        f"`{r.head[:12]}`{_ref(r.head, r.head_ref)}",
        f"- Task: {r.task}" + (f" - claim: {r.claim}" if r.claim else ""),
        f"- Files changed: {len(r.files)}, commits: {len(r.commits)}",
        f"- Generated: {report.created_at}; mode: {report.stats.get('mode', '?')}",
        "",
    ]
    if report.errors:
        lines += [
            f"> **Incomplete review.** {len(report.errors)} agent(s) failed; "
            "their findings are missing:",
            "",
            *(f"> - {e}" for e in report.errors),
            "",
        ]
    lines += [
        "## Summary",
        "",
        "| confirmed | unverified | rejected |",
        "|-|-|-|",
        f"| {counts['confirmed']} | {counts['unverified']} | {counts['rejected']} |",
        "",
    ]
    if report.bench:
        b = report.bench
        lines += ["## Test bench", "", f"Delta: **{b.delta}**", ""]
        for res in (b.base, b.head):
            if res is None:
                continue
            status = (
                "skipped: " + res.reason
                if res.skipped
                else ("pass" if res.passed else f"fail (exit {res.exit_code})")
            )
            lines.append(f"- {res.ref}: `{res.command or '-'}` -> {status}")
        lines.append("")
    findings = sorted(
        report.findings,
        key=lambda v: (_ORDER[v.verdict], _SEV.get(v.finding.severity.value, 9)),
    )
    lines += ["## Findings", ""]
    if not findings:
        lines.append("No findings.")
    for vf in findings:
        f = vf.finding
        loc = f"{f.location.path}:{f.location.line or '?'}" + (
            f" ({f.location.symbol})" if f.location.symbol else ""
        )
        lines += [
            f"### [{vf.verdict.value.upper()}] {f.id}: {f.title}",
            "",
            f"- Severity: **{f.severity}**, category: {f.category}, "
            f"confidence: {f.confidence:.2f}",
            f"- Location: `{loc}`"
            + ("" if f.grounded else " (not grounded: outside the change or the file)"),
            f"- Source: {f.source}"
            + (f" ({f.scanner_rule})" if f.scanner_rule else "")
            + (
                f"; agreement {f.agreement}: {', '.join(f.merged_from)}"
                if f.agreement > 1
                else ""
            ),
        ]
        if f.code_quote:
            lines.append(f"- Code: `{f.code_quote[:160]}`")
        if f.cwe:
            lines.append(f"- CWE: {', '.join(f.cwe)}")
        lines += ["", f.description.strip() or "(no description)", ""]
        if f.rationale:
            lines += ["**Rationale.** " + f.rationale.strip(), ""]
        if vf.prior:
            seen = "; ".join(
                f"{p.get('run_id')} agent {p.get('verdict')}"
                + (f", human {p['human_verdict']}" if p.get("human_verdict") else "")
                for p in vf.prior
            )
            lines += [f"**Seen before.** {seen}", ""]
        if f.reproduction:
            lines += [
                "**Reproduction.**",
                "",
                "```",
                _clip(f.reproduction, 2000),
                "```",
                "",
            ]
        if vf.validator_notes:
            lines += ["**Validator.** " + vf.validator_notes.strip(), ""]
        for ev in vf.evidence:
            lines.append(
                f"- Evidence ({ev.kind}{', ' + ev.ref if ev.ref else ''}): {ev.summary}"
            )
            if ev.command:
                lines.append(
                    f"  - command: `{ev.command}`"
                    + (f" (exit {ev.exit_code})" if ev.exit_code is not None else "")
                )
            if ev.output:
                lines += [
                    "",
                    "  ```",
                    *("  " + line for line in _clip(ev.output, 1200).splitlines()),
                    "  ```",
                    "",
                ]
        fix = vf.fix or f.suggested_fix
        if fix:
            fence = (
                "```diff"
                if fix.lstrip().startswith(("---", "diff", "@@", "+", "-"))
                else "```"
            )
            lines += ["**Suggested fix.**", "", fence, _clip(fix, 3000), "```", ""]
    s = report.scanner
    lines += [
        "## Static analysis",
        "",
        (
            f"Scanners run: {', '.join(s.scanners_run) or 'none'}; "
            f"missing: {', '.join(s.scanners_missing) or 'none'}; "
            f"raw findings: {len(s.findings)}"
        ),
    ]
    if s.errors:
        lines += ["", "Errors:", *(f"- {e}" for e in s.errors)]
    lines += [
        "",
        "## Change blast radius",
        "",
        report.graph.summary or "(no graph)",
        "",
    ]
    for key, callers in list(report.graph.callers.items())[:30]:
        lines.append(
            f"- `{key}` called from: "
            + ", ".join(f"`{c.path}:{c.line}` {c.name}" for c in callers[:8])
        )
    lines += [
        "",
        "## Run statistics",
        "",
        "```",
        *(f"{k}: {v}" for k, v in sorted(report.stats.items())),
        "```",
        "",
    ]
    return "\n".join(lines)


def write_report(report: EvidenceReport, out_dir: Path) -> tuple[Path, Path]:
    """Write report.md, report.json and report.html into ``out_dir``.

    report.html is the self-contained page people can open or be sent.

    Returns:
        The paths of report.md and report.json.
    """
    from scr_agents.ws3_harness.report_html import render_html

    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "report.md"
    js = out_dir / "report.json"
    md.write_text(render_markdown(report))
    js.write_text(report.model_dump_json(indent=2))
    (out_dir / "report.html").write_text(render_html(report), encoding="utf-8")
    return md, js
