"""The evidence report as one self-contained HTML page.

It shows what was reviewed, every finding with the validator's evidence and fix, and
how the review was done. Written next to report.json as report.html and served by the
console (`/api/runs/<run>/report.html`; `?download=1` for a copy to send around).

Findings quote repository content and model output, so the page is rendered here with
every value escaped; the inline script only filters and expands what is already on the
page. A Content-Security-Policy with a per-page nonce blocks anything else (no external
loads, no other script), and the page sends no referrer when someone follows its CWE
links.
"""

from __future__ import annotations

import html
import re
import secrets
from collections import Counter
from pathlib import Path
from typing import Any

from scr_agents.models import EvidenceReport, ValidatedFinding, Verdict

SEVERITIES = ["critical", "high", "medium", "low", "info"]
_ORDER = {Verdict.CONFIRMED: 0, Verdict.UNVERIFIED: 1, Verdict.REJECTED: 2}
_SEV = {s: i for i, s in enumerate(SEVERITIES)}
_CWE = re.compile(r"^CWE-(\d{1,5})$", re.IGNORECASE)
_CODE = re.compile(r"`([^`\n]{1,300})`")
_BOLD = re.compile(r"\*\*([^*\n]{1,300})\*\*")
_STRENGTH = {
    "differential": "Differential: base and head behave differently",
    "executed": "Executed: the validator ran code that shows it",
    "traced": "Traced: the validator cites the code path",
    "none": "No direct evidence recorded",
}


def e(value: Any) -> str:
    """Escape ``value`` for HTML text and quoted attributes; ``None`` becomes empty."""
    return html.escape("" if value is None else str(value), quote=True)


def inline(text: str | None) -> str:
    """One line of model-written text: `code` and **bold**, everything else escaped."""
    s = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", e(text))
    return _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", s)


def prose(text: str | None) -> str:
    """Model-written paragraphs (blank-line separated), each through inline()."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    return "".join("<p>" + inline(p).replace("\n", "<br>") + "</p>" for p in paras)


def clip(text: str | None, n: int) -> str:
    """Cut ``text`` to ``n`` characters, noting how many more are in report.json."""
    text = text or ""
    return (
        text
        if len(text) <= n
        else text[:n] + f"\n… [{len(text) - n} more characters in report.json]"
    )


def code_block(text: str | None, n: int = 6000) -> str:
    """Render ``text``, clipped to ``n`` characters, as a ``<pre>`` block.

    A unified diff gets its lines coloured; anything else is plain preformatted text.
    """
    t = clip(text, n)
    looks_diff = bool(re.search(r"^(@@|--- |\+\+\+ )", t, re.MULTILINE))
    if not looks_diff:
        return f"<pre>{e(t)}</pre>"
    rows = []
    for line in t.splitlines():
        cls = (
            "hunk"
            if line.startswith("@@")
            else "meta"
            if line.startswith(("---", "+++", "diff ", "index "))
            else "add"
            if line.startswith("+")
            else "del"
            if line.startswith("-")
            else "ctx"
        )
        rows.append(
            f'<span class="{cls}">{e(line) or " "}</span>'
        )  # block spans: one per line, no newlines between
    return '<pre class="diff">' + "".join(rows) + "</pre>"


def cwe_links(cwes: list[str]) -> str:
    """Render CWE IDs as tags linking to MITRE's definition; others as plain tags."""
    out = []
    for c in cwes:
        m = _CWE.match(c.strip())
        if m:
            out.append(
                '<a class="tag cwe" '
                f'href="https://cwe.mitre.org/data/definitions/{m.group(1)}.html" '
                f'rel="noopener noreferrer" target="_blank">{e(c.upper())}</a>'
            )
        else:
            out.append(f'<span class="tag">{e(c)}</span>')
    return "".join(out)


def _ref(sha: str, ref: str) -> str:
    return "" if not ref or sha.startswith(ref) or ref.startswith(sha) else f" ({ref})"


def _repo_name(report: EvidenceReport) -> str:
    path = report.request.repo_path.rstrip("/")
    name = path.rsplit("/", 1)[-1]
    return re.sub(r"-[0-9a-f]{8}$", "", name) or path  # clone cache dirs end in -<hash>


def _human(verdicts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for v in verdicts:
        latest[str(v.get("finding_id"))] = v
    return latest


def _finding(vf: ValidatedFinding, human: dict[str, Any] | None, open_: bool) -> str:
    f = vf.finding
    loc = f"{f.location.path}:{f.location.line or '?'}" + (
        f"-{f.location.end_line}" if f.location.end_line else ""
    )
    search = " ".join(
        [
            f.id,
            f.title,
            f.location.path,
            f.category.value,
            f.severity.value,
            vf.verdict.value,
            *f.cwe,
            f.location.symbol or "",
            f.scanner_rule or "",
            f.description[:400],
        ]
    ).lower()
    head = (
        f'<span class="verdict {e(vf.verdict.value)}">{e(vf.verdict.value)}</span>'
        f'<span class="sev {e(f.severity.value)}">{e(f.severity.value)}</span>'
        f'<span class="ftitle">{e(f.title)}</span>'
        f'<span class="floc">{e(loc)}</span>'
    )
    meta = [
        f'<span class="tag">{e(f.category.value)}</span>',
        cwe_links(f.cwe),
        f'<span class="tag" title="{e(_STRENGTH.get(vf.evidence_strength, ""))}">'
        f"evidence: {e(vf.evidence_strength)}</span>",
        f'<span class="tag">confidence {f.confidence:.2f}</span>',
    ]
    if f.agreement > 1:
        meta.append(
            f'<span class="tag" title="{e(", ".join(f.merged_from))}">'
            f"reported by {f.agreement} sources</span>"
        )
    if vf.carried_from:
        meta.append(
            f'<span class="tag" title="settled in run {e(vf.carried_from)}">'
            f"settled in round {vf.round}</span>"
        )
    elif vf.round > 1:
        meta.append(f'<span class="tag">round {vf.round}</span>')
    if not f.grounded:
        meta.append(
            '<span class="tag warn">not grounded: outside the change or the file</span>'
        )
    if human:
        meta.append(
            f'<span class="tag human {e(human.get("verdict"))}">'
            f"human: {e(human.get('verdict'))}</span>"
        )
    body = [
        f'<div class="tags">{"".join(meta)}</div>',
        f'<div class="src">{e(f.id)} · found by {e(f.source)}'
        + (f" · rule {e(f.scanner_rule)}" if f.scanner_rule else "")
        + (f" · in <code>{e(f.location.symbol)}</code>" if f.location.symbol else "")
        + "</div>",
    ]
    if f.code_quote:
        body.append(
            f'<div class="quote"><span class="qloc">{e(loc)}</span>'
            f"<code>{e(f.code_quote)}</code></div>"
        )
    body.append(
        "<h4>What is wrong</h4>"
        f"{prose(f.description) or '<p class=muted>No description.</p>'}"
    )
    if f.rationale:
        body.append(f"<h4>Why it matters</h4>{prose(f.rationale)}")
    if human:
        body.append(
            f'<div class="callout human"><b>Human review:</b> {e(human.get("verdict"))}'
            + (f" · {e(human.get('note'))}" if human.get("note") else "")
            + (
                ' <span class="muted">'
                f"({e(str(human.get('ts', ''))[:16].replace('T', ' '))})</span></div>"
            )
        )
    body.append(
        "<h4>How it was checked</h4>"
        f'<div class="callout {e(vf.verdict.value)}">'
        f"<b>{e(vf.verdict.value.capitalize())}.</b> "
        f"{e(_STRENGTH.get(vf.evidence_strength, vf.evidence_strength))}.</div>"
    )
    if vf.validator_notes:
        body.append(prose(vf.validator_notes))
    if vf.evidence:
        items = []
        for ev in vf.evidence:
            status = (
                ""
                if ev.exit_code is None
                else f'<span class="exit {"ok" if ev.exit_code == 0 else "bad"}">'
                f"exit {ev.exit_code}</span>"
            )
            cmd = f"<code>{e(ev.command)}</code>" if ev.command else ""
            out = (
                f"<details><summary>output ({len(ev.output)} chars)</summary>"
                f"{code_block(ev.output, 4000)}</details>"
                if ev.output
                else ('<div class="muted small">no output</div>' if ev.command else "")
            )
            items.append(
                f'<li><div class="evh"><span class="tag">{e(ev.kind)}'
                f"{(' · ' + e(ev.ref)) if ev.ref else ''}</span>"
                f"{cmd}{status}</div>{
                    f'<div>{inline(ev.summary)}</div>'
                    if ev.summary and not ev.command
                    else ''
                }{out}</li>"
            )
        body.append(f'<ol class="evidence">{"".join(items)}</ol>')
    if f.reproduction:
        body.append(f"<h4>Reproduce it</h4>{code_block(f.reproduction, 3000)}")
    fix = vf.fix or f.suggested_fix
    if fix:
        body.append(
            "<h4>"
            f"{'Fix (checked by the validator)' if vf.fix else 'Suggested fix'}</h4>"
            f"{code_block(fix, 6000)}"
        )
    if vf.prior:
        rows = "".join(
            f"<li>{e(str(p.get('ts', ''))[:10])} · {e(p.get('run_id'))} · "
            f"agent {e(p.get('verdict'))}"
            + (
                f" · human {e(p.get('human_verdict'))}"
                + (f" ({e(p.get('human_note'))})" if p.get("human_note") else "")
                if p.get("human_verdict")
                else ""
            )
            + "</li>"
            for p in vf.prior
        )
        body.append(f'<h4>Seen in earlier reviews</h4><ul class="prior">{rows}</ul>')
    if f.references:
        body.append(
            "<h4>References</h4><ul>"
            + "".join(f"<li>{e(r)}</li>" for r in f.references)
            + "</ul>"
        )
    return (
        f'<article class="finding" id="{e(f.id)}" '
        f'data-verdict="{e(vf.verdict.value)}" data-sev="{e(f.severity.value)}" '
        f'data-cat="{e(f.category.value)}" data-text="{e(search)}">'
        f"<details{' open' if open_ else ''}>"
        f'<summary>{head}</summary><div class="fbody">{"".join(body)}</div>'
        "</details></article>"
    )


def _bench(report: EvidenceReport) -> str:
    b = report.bench
    if b is None:
        return "<p class=muted>The test bench did not run.</p>"
    rows = []
    for res in (b.base, b.head):
        if res is None:
            continue
        status = (
            ("skipped: " + res.reason)
            if res.skipped
            else ("passed" if res.passed else f"failed (exit {res.exit_code})")
        )
        cls = "muted" if res.skipped else ("ok" if res.passed else "bad")
        tail = (res.stdout or "")[-3000:] + (
            "\n" + res.stderr[-2000:] if res.stderr else ""
        )
        rows.append(
            f"<tr><td>{e(res.ref)}</td>"
            f"<td><code>{e(res.command or '-')}</code></td>"
            f'<td class="{cls}">{e(status)}</td>'
            f"<td>{res.duration_s:.1f}s</td><td>{
                '<details><summary>output</summary>'
                + code_block(tail, 5000)
                + '</details>'
                if tail.strip()
                else ''
            }</td></tr>"
        )
    return (
        f"<p><b>{e(b.delta or 'no comparison')}</b></p>"
        "<table><tr><th>commit</th><th>command</th><th>result</th>"
        f"<th>time</th><th></th></tr>{''.join(rows)}</table>"
    )


def report_filename(report: EvidenceReport) -> str:
    """scr-agents-<repo>-<run>.html, safe for a Content-Disposition header."""
    return (
        re.sub(
            r"[^A-Za-z0-9._-]+", "_", f"scr-agents-{_repo_name(report)}-{report.run_id}"
        )[:120]
        + ".html"
    )


def csp(nonce: str) -> str:
    """Build the page's Content-Security-Policy around the script ``nonce``.

    The page's policy, for the response header too (frame-ancestors only works there).
    """
    return (
        "default-src 'none'; style-src 'unsafe-inline'; "
        f"script-src 'nonce-{nonce}'; img-src data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


# The page's stylesheet and script, inlined into every page (it loads nothing else)
_ASSETS = Path(__file__).parent / "report_assets"
CSS = (_ASSETS / "report.css").read_text(encoding="utf-8")

SCRIPT = (_ASSETS / "report.js").read_text(encoding="utf-8")


def scrub_paths(value: Any, prefixes: dict[str, str]) -> Any:
    """Replace each host path prefix with its label in the strings of ``value``.

    Strings inside lists and dict values are replaced too (dict keys are kept), longest
    prefix first, so tracebacks and commands in a copy for others do not show this
    machine's layout.
    """
    if isinstance(value, str):
        for prefix in sorted(prefixes, key=len, reverse=True):
            if prefix and prefix in value:
                value = value.replace(prefix, prefixes[prefix])
        return value
    if isinstance(value, dict):
        return {k: scrub_paths(v, prefixes) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_paths(v, prefixes) for v in value]
    return value


def host_prefixes(
    report: EvidenceReport, run_dir: str | None = None, output_dir: str | None = None
) -> dict[str, str]:
    """Map this machine's paths that end up in a report to the labels that hide them.

    The run directory, output directory, repository and home directory map to
    ``<run>``, ``<out>``, ``<repo>`` and ``~``, each under both the given path and its
    real path (/tmp is /private/tmp on macOS).
    """
    import os

    out: dict[str, str] = {}
    for path, label in (
        (run_dir, "<run>"),
        (output_dir, "<out>"),
        (report.request.repo_path, "<repo>"),
        (os.path.expanduser("~"), "~"),
    ):
        if path and len(path) > 1:
            out[path.rstrip("/")] = label
            out[os.path.realpath(path).rstrip("/")] = label
    return out


def render_html(
    report: EvidenceReport,
    human_verdicts: list[dict[str, Any]] | None = None,
    shared: bool = False,
    nonce: str | None = None,
    scrub: dict[str, str] | None = None,
    label: str = "review report",
) -> str:
    """Render the evidence report as one self-contained HTML page.

    Args:
        report: The report to render.
        human_verdicts: Human review records; the last one for each ``finding_id`` is
            shown on that finding.
        shared: Render a copy that leaves this machine: it hides host paths, and
            replaces the ``scrub`` prefixes (see ``host_prefixes``) everywhere in the
            report.
        nonce: The script nonce. Pass it when the response also carries the
            Content-Security-Policy header (see ``csp``); otherwise a random one is
            used.
        scrub: Extra path prefixes to replace in a ``shared`` copy, mapped to their
            labels.
        label: What the page is, shown in its header and title, such as
            ``Review Report (round 2)``.

    Returns:
        The HTML document.
    """
    if shared:
        repo_label = _repo_name(report)
        prefixes = {**host_prefixes(report), **(scrub or {})}
        report = EvidenceReport.model_validate(
            scrub_paths(report.model_dump(mode="json"), prefixes)
        )
        human_verdicts = scrub_paths(human_verdicts or [], prefixes)
        report.request.repo_path = repo_label
    r = report.request
    counts = report.counts()
    human = _human(human_verdicts or [])
    for v in report.findings:  # carried findings bring the verdict that settled them
        if v.human_verdict and v.finding.id not in human:
            human[v.finding.id] = {"verdict": v.human_verdict, "note": v.human_note}
    findings = sorted(
        report.findings,
        key=lambda v: (
            _ORDER[v.verdict],
            _SEV.get(v.finding.severity.value, 9),
            v.finding.location.path,
        ),
    )
    confirmed = [v for v in findings if v.verdict == Verdict.CONFIRMED]
    sev_counts = Counter(v.finding.severity.value for v in confirmed)
    nonce = nonce or secrets.token_urlsafe(12)
    repo = _repo_name(report)
    scope = (
        "whole repository at head"
        if r.scope == "all"
        else f"{r.base[:10]}{_ref(r.base, r.base_ref)} → "
        f"{r.head[:10]}{_ref(r.head, r.head_ref)}"
    )
    task = (
        "Verify a fix" + (f": {r.claim}" if r.claim else "")
        if r.task.value == "verify_fix"
        else "Find bugs and vulnerabilities"
    )
    models = report.stats.get("models") or {}
    meta = [
        f"run {e(report.run_id)}",
        f"generated {e(report.created_at[:16].replace('T', ' '))} UTC",
        f"{len(r.files)} files · {len(r.commits)} commits",
        f"mode {e(report.stats.get('mode', '?'))}",
    ]
    if report.stats.get("duration_s") is not None:
        meta.append(f"took {float(report.stats['duration_s']):.0f}s")
    if models:
        meta.append(" · ".join(f"{e(k)} {e(v)}" for k, v in models.items()))

    total = max(1, len(confirmed))
    colors = {
        "critical": "var(--critical)",
        "high": "var(--high)",
        "medium": "var(--medium)",
        "low": "var(--low)",
        "info": "var(--sev-info)",
    }
    bar = "".join(
        f'<span style="width:{100 * sev_counts[s] / total:.1f}%;'
        f'background:{colors[s]}" title="{sev_counts[s]} {s}"></span>'
        for s in SEVERITIES
        if sev_counts[s]
    )
    legend = "".join(
        f'<span><i style="background:{colors[s]}"></i>{sev_counts[s]} {s}</span>'
        for s in SEVERITIES
        if sev_counts[s]
    )
    tally = (
        '<div class="tally">'
        f'<div class="tile confirmed"><div class="n">{counts["confirmed"]}</div>'
        '<div class="l">confirmed</div></div>'
        f'<div class="tile unverified"><div class="n">{counts["unverified"]}</div>'
        '<div class="l">unverified</div></div>'
        f'<div class="tile rejected"><div class="n">{counts["rejected"]}</div>'
        '<div class="l">rejected</div></div>'
        f'<div class="tile sevtile"><div class="l">confirmed by severity</div>'
        + (
            f'<div class="sevbar">{bar}</div><div class="legend">{legend}</div>'
            if confirmed
            else '<p class="muted small">Nothing confirmed.</p>'
        )
        + "</div></div>"
    )
    banners = []
    if report.errors:
        banners.append(
            '<div class="banner bad"><b>Incomplete review.</b> '
            f"{len(report.errors)} agent(s) failed, so their findings are missing:"
            f"<ul>{''.join(f'<li>{e(x)}</li>' for x in report.errors)}</ul></div>"
        )
    if report.round > 1:
        carried = sum(1 for v in report.findings if v.carried_from)
        banners.append(
            f'<div class="banner note"><b>Review round {report.round} of '
            f"{report.max_rounds}.</b> {carried} finding(s) settled in earlier rounds "
            f"are carried; {len(report.replaced)} finding(s) of the previous round "
            f"(run {e(report.previous_run_id)}) were reviewed again.</div>"
        )
    if r.instructions:
        banners.append(
            f'<div class="banner note"><b>Asked for:</b> {e(r.instructions)}</div>'
        )

    key = [
        v for v in confirmed if v.finding.severity.value in ("critical", "high")
    ] or confirmed[:5]
    key_html = (
        "".join(
            f'<a href="#{e(v.finding.id)}">'
            f'<span class="verdict {e(v.verdict.value)}">{e(v.verdict.value)}</span>'
            f'<span class="sev {e(v.finding.severity.value)}">'
            f"{e(v.finding.severity.value)}</span>"
            f'<span>{e(v.finding.title)}<span class="loc">'
            f"{e(v.finding.location.path)}:{e(v.finding.location.line or '?')}"
            "</span></span></a>"
            for v in key
        )
        or '<div class="empty">No confirmed findings. '
        "Unverified ones are listed below; they need a closer look.</div>"
    )

    cats = sorted({v.finding.category.value for v in findings})
    filters = (
        '<div class="filters">'
        + "".join(
            f'<button data-filter="verdict" data-value="{v}" '
            f'aria-pressed="{str(v == "all").lower()}">'
            f"{v.capitalize()}{'' if v == 'all' else f' ({counts[v]})'}</button>"
            for v in ("all", "confirmed", "unverified", "rejected")
        )
        + (
            '<select id="fsev" aria-label="severity">'
            '<option value="all">Any severity</option>'
        )
        + "".join(f'<option value="{s}">{s}</option>' for s in SEVERITIES)
        + "</select>"
        + (
            '<select id="fcat" aria-label="category">'
            '<option value="all">Any category</option>'
        )
        + "".join(f'<option value="{e(c)}">{e(c)}</option>' for c in cats)
        + "</select>"
        + (
            '<input id="fq" type="search" placeholder="Search title, file, CWE…" '
            'aria-label="search findings">'
        )
        + (
            '<button id="fexpand">Expand all</button>'
            '<span class="count" id="fcount"></span></div>'
        )
    )
    cards = (
        "".join(
            _finding(
                v,
                human.get(v.finding.id),
                open_=v.verdict == Verdict.CONFIRMED and i < 12,
            )
            for i, v in enumerate(findings)
        )
        or '<div class="empty">The agents reported no findings.</div>'
    )

    # how the review was done, in pipeline order
    p, s, g = report.plan, report.scanner, report.graph
    hyps = "".join(f"<li>{inline(h)}</li>" for h in p.hypotheses)
    focus = ", ".join(f"<code>{e(x)}</code>" for x in p.focus_files[:20])
    sym = "".join(
        f"<li><code>{e(x.name)}</code> <span class=muted>{e(x.path)}:{x.line}</span>"
        + (
            f" · called from {len(g.callers.get(x.name, []))} place(s)"
            if g.callers.get(x.name)
            else ""
        )
        + "</li>"
        for x in g.changed_symbols[:40]
    )
    hits = "".join(
        f"<tr><td>{e(h.scanner)}</td><td><code>{e(h.rule)}</code></td>"
        f'<td class="sev {e(h.severity)}">{e(h.severity)}</td>'
        f"<td>{e(h.path)}:{e(h.line or '?')}</td>"
        f"<td>{e(clip(h.message, 200))}</td></tr>"
        for h in s.findings[:80]
    )
    strengths = Counter(v.evidence_strength for v in report.findings)
    steps = [
        (
            "Read the change",
            f"<p>{len(r.files)} files and {len(r.commits)} commits ({e(scope)}).</p>",
        ),
        (
            "Mapped the code",
            f"<p>{e(g.summary or 'No code graph.')}</p>"
            + (
                f"<details><summary>{len(g.changed_symbols)} changed symbols</summary>"
                f"<ul>{sym}</ul></details>"
                if sym
                else ""
            ),
        ),
        (
            "Ran the static scanners",
            f"<p>Ran {e(', '.join(s.scanners_run) or 'none')}"
            + (
                f"; not installed: {e(', '.join(s.scanners_missing))}"
                if s.scanners_missing
                else ""
            )
            + f". {len(s.findings)} raw hits.</p>"
            + (
                "<details><summary>scanner hits</summary><table><tr><th>scanner</th>"
                "<th>rule</th><th>severity</th><th>where</th><th>message</th></tr>"
                f"{hits}</table></details>"
                if hits
                else ""
            )
            + ("".join(f"<p class='muted small'>{e(x)}</p>" for x in s.errors)),
        ),
        ("Built and tested base and head", _bench(report)),
        (
            "Planned the review",
            (f"<p>Focus: {focus}</p>" if focus else "")
            + (
                f"<ol>{hyps}</ol>"
                if hyps
                else "<p class=muted>No hypotheses recorded.</p>"
            )
            + (prose(p.notes) if p.notes else ""),
        ),
        (
            "Reviewed in parallel",
            (
                "<p>Bug, security and style reviewers read the code with tools "
                "(files, diff, code graph, scanner hits, knowledge base). "
                f"{len(report.findings)} distinct findings after merging "
                "duplicates.</p>"
            ),
        ),
        (
            "Re-checked every finding with a second model",
            "<p>The validator rebuilt the code in a sandbox, ran commands and proofs "
            "of concept, and gave each finding a verdict. Evidence: "
            + (", ".join(f"{n} {e(k)}" for k, n in strengths.most_common()) or "none")
            + ".</p>",
        ),
    ]
    steps_html = "".join(
        f'<div class="step"><h3>{e(t)}</h3>{b}</div>' for t, b in steps
    )

    files = "".join(
        f"<tr><td><code>{e(f.path)}</code>{
            (' <span class=muted>(from ' + e(f.old_path) + ')</span>')
            if f.old_path
            else ''
        }</td>"
        f"<td>{e(f.status)}</td><td>{e(f.language or '')}</td>"
        f"<td class=ok>+{f.added}</td><td class=bad>-{f.removed}</td></tr>"
        for f in r.files[:400]
    )
    commits = "".join(
        f"<tr><td><code>{e(c.sha[:10])}</code></td><td>{e(c.subject)}</td>"
        f"<td>{e(c.author)}</td><td>{e(c.date[:10])}</td></tr>"
        for c in r.commits[:100]
    )
    stats_rows = "".join(
        f"<tr><td>{e(k)}</td><td><code>{e(v)}</code></td></tr>"
        for k, v in sorted(report.stats.items())
    )
    pr = (
        (
            f"<p>Pull request #{e(r.pr.number)}: {e(r.pr.title)}"
            + (f" by {e(r.pr.author)}" if r.pr.author else "")
            + "</p>"
        )
        if r.pr
        else ""
    )

    share_note = (
        "This report describes security weaknesses and how to trigger them; "
        "share it with the people who will fix them."
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
<title>{e(repo)} · {e(label)} · {counts["confirmed"]} confirmed</title>
<style>{CSS}</style>
</head>
<body>
<header class="hero"><div class="wrap">
  <div class="brand">SCR Agents · {e(label)}</div>
  <h1>{e(repo)}</h1>
  <div class="sub">{e(task)} · <code>{e(scope)}</code></div>
  <div class="meta">{"".join(f"<span>{m}</span>" for m in meta)}</div>
  {tally}
  {"".join(banners)}
</div></header>
<nav class="toc" aria-label="sections"><div class="wrap">
  <a href="#summary">Summary</a><a href="#findings">Findings ({
        len(findings)
    })</a><a href="#process">How it was checked</a>
  <a href="#scope">What was reviewed</a><a href="#run">Run details</a>
</div></nav>
<main class="wrap">
<section id="summary"><h2>Key findings</h2><div class="key">{key_html}</div>
  {
        f'<p class="muted small">Test bench: {e(report.bench.delta)}</p>'
        if report.bench and report.bench.delta
        else ""
    }</section>
<section id="findings"><h2>All findings</h2>{filters if findings else ""}{
        cards
    }</section>
<section id="process"><h2>How it was checked</h2><div class="steps">{
        steps_html
    }</div></section>
<section id="scope"><h2>What was reviewed</h2>
  <p>Repository <code>{e(r.repo_path)}</code> · {e(scope)}</p>{pr}
  {
        f"<h3>Files ({len(r.files)})</h3><table><tr><th>path</th><th>status</th>"
        f"<th>language</th><th>added</th><th>removed</th></tr>{files}</table>"
        if files
        else ""
    }
  {
        f"<h3>Commits ({len(r.commits)})</h3><table><tr><th>commit</th>"
        f"<th>subject</th><th>author</th><th>date</th></tr>{commits}</table>"
        if commits
        else ""
    }
</section>
<section id="run"><h2>Run details</h2><table>{stats_rows}</table></section>
<footer>Generated by SCR Agents from {e(report.run_id)}. {e(share_note)}</footer>
</main>
<script nonce="{nonce}">{SCRIPT}</script>
</body>
</html>
"""
