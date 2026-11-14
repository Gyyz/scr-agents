import json

from scr_agents.config import Settings
from scr_agents.pipeline import run_review
from scr_agents.sessions import (
    LEGACY_SESSION,
    ensure_session,
    find_run_dir,
    iter_run_dirs,
    list_sessions,
    new_session,
    session_meta,
    tidy_legacy,
)


def test_sessions_index_and_lookup(demo_repo, tmp_path):
    """Runs land in their session and are found by id; ``cli`` is the default."""
    repo, base, head = demo_repo
    settings = Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "scanners": {"enabled": ["builtin"]},
        }
    )
    sid = new_session(settings, "chat", repo=str(repo))
    assert session_meta(settings, sid)["kind"] == "chat"
    report, run_dir = run_review(settings, repo, base, head, session_id=sid)
    assert run_dir == tmp_path / "out" / "sessions" / sid / report.run_id
    assert find_run_dir(settings, report.run_id) == run_dir
    assert find_run_dir(settings, "run-nope") is None
    sessions = list_sessions(settings)
    assert [s["session_id"] for s in sessions] == [sid] and sessions[0]["runs"] == [
        report.run_id
    ]
    # an implicit session is created when none is given
    report2, run_dir2 = run_review(settings, repo, base, head)
    assert (
        run_dir2.parent.name != sid
        and session_meta(settings, run_dir2.parent.name)["kind"] == "cli"
    )
    assert {d.name for _s, d in iter_run_dirs(settings)} == {
        report.run_id,
        report2.run_id,
    }


def test_tidy_moves_legacy_top_level_runs(tmp_path):
    """Tidying moves top-level runs into the legacy session, and only once."""
    settings = Settings.load(overrides={"output_dir": str(tmp_path / "out")})
    legacy = tmp_path / "out" / "run-deadbeef"
    legacy.mkdir(parents=True)
    (legacy / "request.json").write_text(
        json.dumps(
            {
                "run_id": "run-deadbeef",
                "repo_path": "/x",
                "base_ref": "a",
                "head_ref": "b",
                "base": "a" * 40,
                "head": "b" * 40,
            }
        )
    )
    assert [(s, d.name) for s, d in iter_run_dirs(settings)] == [
        (LEGACY_SESSION, "run-deadbeef")
    ]
    assert tidy_legacy(settings) == ["run-deadbeef"]
    assert not legacy.exists()
    moved = tmp_path / "out" / "sessions" / LEGACY_SESSION / "run-deadbeef"
    assert moved.exists() and find_run_dir(settings, "run-deadbeef") == moved
    assert tidy_legacy(settings) == []
    ensure_session(settings, LEGACY_SESSION)  # idempotent
    assert session_meta(settings, LEGACY_SESSION)["kind"] == "legacy"
