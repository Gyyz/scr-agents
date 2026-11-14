"""Merging, scanner agreement, grounding and ranking of candidate findings.

These steps live in the orchestrator.
"""

from __future__ import annotations

from scr_agents.models import Category, Finding, Location, ScannerFinding, Severity
from scr_agents.ws1_review.orchestrator import (
    absorb_scanner_hits,
    merge_findings,
    rank_key,
)


def _f(
    path,
    line,
    cat="bug",
    sev="medium",
    conf=0.5,
    title="t",
    source="subagent:bugs",
    **kw,
):
    return Finding(
        title=title,
        category=Category(cat),
        severity=Severity(sev),
        confidence=conf,
        source=source,
        location=Location(path=path, line=line),
        **kw,
    )


def test_merge_clusters_nearby_findings_across_categories():
    """Nearby findings merge across categories into the most confident, pooling CWEs."""
    bug = _f("a.py", 9, "bug", conf=0.6, cwe=["CWE-20"])
    vuln = _f(
        "a.py",
        10,
        "vulnerability",
        "high",
        conf=0.9,
        source="subagent:vulnerabilities",
        cwe=["CWE-78"],
    )
    far = _f("a.py", 40, "bug", conf=0.7)
    merged = merge_findings([bug, vuln, far])
    assert len(merged) == 2
    primary = next(f for f in merged if f.location.line == 10)
    assert primary.category == Category.VULNERABILITY and primary.confidence == 0.9
    assert primary.agreement == 2 and primary.merged_from == [
        "subagent:bugs",
        "subagent:vulnerabilities",
    ]
    assert primary.cwe == ["CWE-20", "CWE-78"]
    assert next(f for f in merged if f.location.line == 40).agreement == 1


def test_merge_keeps_distinct_lineless_findings_and_merges_identical_titles():
    """Findings without a line merge only when their titles match, ignoring case."""
    a = _f("b.py", None, title="Missing docstring")
    b = _f("b.py", None, title="Dead code")
    c = _f("b.py", None, title="missing docstring", source="subagent:style")
    merged = merge_findings([a, b, c])
    assert len(merged) == 2 and {f.title for f in merged} == {
        "Missing docstring",
        "Dead code",
    }
    assert next(f for f in merged if f.title == "Missing docstring").agreement == 2


def test_scanner_hits_raise_agreement_or_get_promoted():
    """Scanner hits raise a nearby finding's agreement; lone high ones are promoted."""
    found = merge_findings([_f("a.py", 10, "vulnerability", "high", conf=0.9)])
    hits = [
        ScannerFinding(
            scanner="bandit",
            rule="B602",
            message="shell=True",
            path="a.py",
            line=11,
            severity="high",
            category="vulnerability",
            cwe=["CWE-78"],
        ),
        ScannerFinding(
            scanner="semgrep",
            rule="x",
            message="also here",
            path="a.py",
            line=12,
            severity="high",
            category="vulnerability",
        ),
        ScannerFinding(
            scanner="cppcheck",
            rule="bufferAccessOutOfBounds",
            message="oob",
            path="c.c",
            line=3,
            severity="high",
            category="bug",
        ),
        ScannerFinding(
            scanner="ruff",
            rule="E501",
            message="long line",
            path="d.py",
            line=1,
            severity="low",
            category="style",
        ),
    ]
    out = absorb_scanner_hits(found, hits)
    near = next(f for f in out if f.location.path == "a.py")
    assert (
        near.agreement == 3
        and near.scanner_rule == "bandit:B602"
        and "CWE-78" in near.cwe
    )
    assert (
        "scanner:bandit" in near.merged_from and "scanner:semgrep" in near.merged_from
    )
    promoted = [f for f in out if f.source.startswith("scanner:")]
    assert [f.location.path for f in promoted] == ["c.c"], (
        "only uncovered critical/high hits are promoted"
    )


def test_candidates_are_ranked_by_grounding_agreement_then_severity():
    """Agreement and reproductions outrank severity; ungrounded findings rank last."""
    grounded_low = _f("a.py", 1, sev="low", conf=0.9)
    ungrounded_critical = _f("a.py", 2, sev="critical", conf=0.9, grounded=False)
    agreed_medium = _f("a.py", 3, sev="medium", conf=0.4, agreement=3)
    repro_medium = _f("a.py", 4, sev="medium", conf=0.4, reproduction="python3 poc.py")
    order = sorted(
        [ungrounded_critical, grounded_low, repro_medium, agreed_medium], key=rank_key
    )
    assert order == [agreed_medium, repro_medium, grounded_low, ungrounded_critical]


def test_merge_files_the_spot_as_a_vulnerability_when_any_report_is_one():
    """A more confident bug report does not turn a security finding into a bug."""
    bug = _f("a.py", 9, "bug", "critical", conf=0.95)
    vuln = _f(
        "a.py",
        10,
        "vulnerability",
        "high",
        conf=0.7,
        source="subagent:vulnerabilities",
        cwe=["CWE-78"],
    )
    (merged,) = merge_findings([bug, vuln])
    assert merged.confidence == 0.95 and merged.location.line == 9
    assert merged.category == Category.VULNERABILITY and merged.cwe == ["CWE-78"]
