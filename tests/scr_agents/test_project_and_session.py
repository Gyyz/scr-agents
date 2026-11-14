import io

from rich.console import Console

from scr_agents.config import Settings
from scr_agents.inputs.project import (
    default_scope,
    describe_repo,
    is_url,
    resolve_project,
)
from scr_agents.inputs.request import build_request
from scr_agents.tui.render import LiveRenderer
from scr_agents.tui.session import ChatSession


def test_resolve_local_path_and_clone_from_url(demo_repo, tmp_path):
    """A path inside a repo resolves to its top level; a URL is cloned and reused."""
    repo, _base, head = demo_repo
    assert (
        resolve_project(str(repo / "app"), tmp_path / "projects") == repo
    )  # any path inside -> toplevel
    assert (
        not is_url(str(repo)) and is_url("https://github.com/x/y.git") and is_url("x/y")
    )
    url = f"file://{repo}"
    clone = resolve_project(url, tmp_path / "projects")
    assert (
        clone.parent == (tmp_path / "projects").resolve()
        and clone.name.startswith("demo-")
        and (clone / ".git").exists()
    )
    assert describe_repo(clone)["head"] == head
    assert (
        resolve_project(url, tmp_path / "projects") == clone
    )  # second call reuses (and fetches)
    assert default_scope(clone) == ("HEAD~1", "HEAD", "range")


def test_clone_cache_is_keyed_by_full_url_and_follows_the_remote(demo_repo, tmp_path):
    """Same-named repos at two URLs get separate clones, which fetch new commits."""
    import shutil
    import subprocess

    repo, _base, head = demo_repo
    alice, bob = tmp_path / "alice" / "demo", tmp_path / "bob" / "demo"
    shutil.copytree(repo, alice)
    shutil.copytree(repo, bob)
    (bob / "BOB").write_text("bob")
    subprocess.run(["git", "-C", str(bob), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(bob),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "bob",
        ],
        check=True,
    )
    projects = tmp_path / "projects"
    a = resolve_project(f"file://{alice}", projects)
    b = resolve_project(f"file://{bob}", projects)
    assert a != b and not (a / "BOB").exists() and (b / "BOB").exists()

    (alice / "NEW").write_text("new")
    subprocess.run(["git", "-C", str(alice), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(alice),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "new",
        ],
        check=True,
    )
    new_head = subprocess.run(
        ["git", "-C", str(alice), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert new_head != head
    assert (
        describe_repo(resolve_project(f"file://{alice}", projects))["head"] == new_head
    )


def test_whole_repository_scope(demo_repo):
    """Scope ``all`` reviews every file as added, with all of its lines changed."""
    repo, _base, head = demo_repo
    req = build_request(
        repo, "HEAD", "HEAD", scope="all", instructions="audit everything", max_files=50
    )
    assert req.scope == "all" and req.instructions == "audit everything"
    paths = {f.path for f in req.files}
    assert {"app/utils.py", "native/greet.c", "tests/test_utils.py"} <= paths
    utils = req.file("app/utils.py")
    assert utils.status == "added" and utils.changed_lines() == list(
        range(1, utils.added + 1)
    )
    assert req.commits and req.commits[0].sha == head


def test_live_renderer_prints_claude_code_style_lines():
    """The live renderer prints one plain line per stage, tool call and verdict."""
    out = io.StringIO()
    console = Console(file=out, width=120, force_terminal=False, color_system=None)
    r = LiveRenderer(console)
    with r:
        r.handle(
            {
                "event": "request",
                "run_id": "run-1",
                "files": 3,
                "commits": 1,
                "task": "find_bugs",
            }
        )
        r.handle({"event": "stage", "stage": "graph", "status": "running"})
        r.handle({"event": "stage", "stage": "graph", "status": "done", "symbols": 6})
        r.handle({"event": "subagent_start", "agent": "subagent:bugs", "model": "m"})
        r.handle(
            {
                "event": "tool_call",
                "agent": "subagent:bugs",
                "tool": "read_file",
                "input": {"path": "a.py"},
                "output_chars": 120,
                "duration_s": 0.01,
                "is_error": False,
            }
        )
        r.handle(
            {
                "event": "verdict",
                "agent": "validator",
                "finding": "F-1",
                "verdict": "confirmed",
            }
        )
    text = out.getvalue()
    assert "review run-1" in text and "graph done" in text and "symbols=6" in text
    assert "read_file(path=a.py)" in text and "120 chars" in text
    assert "validator:F-1" in text and "confirmed" in text


def test_interactive_session_offline(demo_repo, tmp_path):
    """An offline chat session reviews, answers and handles its slash commands."""
    repo, _base, _head = demo_repo
    out = io.StringIO()
    console = Console(file=out, width=140, force_terminal=False, color_system=None)
    settings = Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "scanners": {"enabled": ["builtin"]},
        }
    )
    s = ChatSession(settings, str(repo), console=console)
    assert (s.base, s.head, s.scope) == (
        "HEAD~1",
        "HEAD",
        "range",
    ) and s.mode == "offline"

    assert s.handle("check the last 1 commit for security problems") is True
    assert s.last_report is not None and s.last_report.request.instructions.startswith(
        "check the last 1 commit"
    )
    assert (
        s.last_run_dir.parent == s.session_dir
        and (s.session_dir / "session.json").exists()
    )
    text = out.getvalue()
    assert (
        "subagent:vulnerabilities" in text
        and "scanner_findings" in text
        and "Findings for" in text
    )
    assert "Review run-" in text and "confirmed" in text  # lead agent reply

    fid = s.last_report.findings[0].finding.id
    out.truncate(0)
    out.seek(0)
    s.handle(f"/show {fid}")
    assert fid in out.getvalue() and "evidence" in out.getvalue()
    out.truncate(0)
    out.seek(0)
    s.handle("/findings")
    s.handle("/log 5")
    s.handle("/runs")
    assert fid in out.getvalue() and "events in" in out.getvalue()
    s.handle(f"/verdict {fid} accept looks right")
    assert "recorded accept" in out.getvalue()

    out.truncate(0)
    out.seek(0)
    s.handle("/scope all")
    assert s.scope == "all"
    s.handle("/review")
    assert (
        s.last_report.request.scope == "all" and len(s.last_report.request.files) >= 3
    )
    assert s.handle("/quit") is False
    s.log.close()
