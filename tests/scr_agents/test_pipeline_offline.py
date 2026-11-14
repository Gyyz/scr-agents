from scr_agents.config import Settings
from scr_agents.models import Verdict
from scr_agents.pipeline import run_review
from scr_agents.ws5_knowledge.activity_log import ActivityLog
from scr_agents.ws5_knowledge.human_review import load_verdicts, record_verdict


def test_offline_end_to_end(demo_repo, tmp_path):
    """An offline review confirms the planted defects and writes its reports."""
    repo, base, head = demo_repo
    settings = Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "scanners": {"enabled": ["builtin"]},
        }
    )
    report, run_dir = run_review(settings, repo, base, head)

    assert (run_dir / "report.md").exists() and (run_dir / "report.json").exists()
    assert (run_dir / "human_review.md").exists()
    confirmed = [vf for vf in report.findings if vf.verdict == Verdict.CONFIRMED]
    assert confirmed, (
        "scanner-backed findings should be confirmed by the offline validator"
    )
    assert any(
        "strcpy" in vf.finding.title.lower() or "C-STRCPY" in vf.finding.title
        for vf in confirmed
    )
    assert any("shell" in vf.finding.title.lower() for vf in confirmed)
    assert all(ev.kind == "test_run" for vf in confirmed for ev in vf.evidence[:1])
    assert report.bench is not None and report.bench.base is not None
    assert report.bench.delta in (
        "regression: base passes, head fails",
        "both fail",
        "both pass",
    ) or report.bench.delta.startswith("skipped")
    assert set(report.stats["prompts_used"]) >= {
        "review_planner",
        "subagent_bugs",
        "subagent_vulnerabilities",
        "subagent_style",
        "validator",
    }

    events = ActivityLog.read(run_dir / "activity.jsonl")
    kinds = {e["event"] for e in events}
    assert {
        "request",
        "code_graph",
        "scanners",
        "test_bench",
        "plan",
        "llm_call",
        "tool_call",
        "verdict",
        "report",
    } <= kinds
    assert all(head in e["commit_ids"] and base in e["commit_ids"] for e in events)

    fid = confirmed[0].finding.id
    rec = record_verdict(run_dir, fid, "accept", "looks right")
    assert rec["finding_id"] == fid and load_verdicts(run_dir)[0]["verdict"] == "accept"
    assert not (run_dir / "worktrees").exists(), "worktrees are cleaned up"
    md = (run_dir / "report.md").read_text()
    assert "[CONFIRMED]" in md and "Test bench" in md


def test_relative_output_dir(demo_repo, tmp_path, monkeypatch):
    """A relative ``output_dir`` resolves from the cwd and checkouts still fill."""
    repo, base, head = demo_repo
    monkeypatch.chdir(tmp_path)
    settings = Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": "out",
            "scanners": {"enabled": ["builtin"]},
        }
    )
    report, run_dir = run_review(settings, repo, base, head)
    assert (
        run_dir.is_absolute() and run_dir.parent.parent == tmp_path / "out" / "sessions"
    )
    assert (
        run_dir.parent.name.startswith("session-")
        and (run_dir.parent / "session.json").exists()
    )
    assert report.request.session_id == run_dir.parent.name
    assert report.scanner.findings, (
        "worktrees must be populated even with a relative output dir"
    )
    assert report.graph.changed_symbols


def test_candidates_event_explains_the_filtering(demo_repo, tmp_path):
    """The ``candidates`` event's counts add up to the findings the run kept.

    The chat's progress narrative reads these fields: raw findings, duplicates merged,
    scanner hits promoted.
    """
    repo, base, head = demo_repo
    settings = Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "scanners": {"enabled": ["builtin"]},
        }
    )
    report, run_dir = run_review(settings, repo, base, head)
    cand = next(
        e
        for e in ActivityLog.read(run_dir / "activity.jsonl")
        if e["event"] == "candidates"
    )
    assert {"raw", "duplicates", "promoted", "ignored", "by_agent", "count"} <= set(
        cand
    )
    assert (
        cand["raw"] - cand["duplicates"] + cand["promoted"] - cand["ignored"]
        == cand["count"]
    )
    assert (
        sum(cand["by_agent"].values()) == cand["raw"]
        and len(report.findings) == cand["count"]
    )
