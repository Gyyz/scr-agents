from scr_agents.inputs.git import parse_unified_diff
from scr_agents.inputs.request import build_request


def test_build_request_from_demo_repo(demo_repo):
    """A request for the demo range lists its files, languages, lines and commit."""
    repo, base, head = demo_repo
    req = build_request(repo, base, head)
    assert req.base == base and req.head == head
    paths = {f.path for f in req.files}
    assert {"app/utils.py", "native/greet.c", "tests/test_utils.py"} <= paths
    greet = req.file("native/greet.c")
    assert greet.status == "added" and greet.language == "c"
    utils = req.file("app/utils.py")
    assert utils.status == "modified" and utils.changed_lines()
    assert len(req.commits) == 1 and "shell strings" in req.commits[0].subject
    assert head in req.commit_ids and base in req.commit_ids


def test_parse_unified_diff_handles_removed_line_starting_with_dashes():
    """A removed ``-- comment`` line is counted, not mistaken for a ``---`` header."""
    diff = """diff --git a/x.sql b/x.sql
--- a/x.sql
+++ b/x.sql
@@ -1,2 +1,2 @@
--- comment
+-- new comment
 select 1;
"""
    files = parse_unified_diff(diff)
    assert len(files) == 1 and files[0].added == 1 and files[0].removed == 1
    assert files[0].changed_lines() == [1]


def _commit_all(repo, message):
    import subprocess

    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            message,
        ],
        check=True,
    )


def test_non_ascii_and_space_file_names_survive_diff_parsing(demo_repo):
    """File names with accents or spaces come through the diff intact."""
    repo, _base, head = demo_repo
    (repo / "café.py").write_text("x = 1\n")
    (repo / "my file.py").write_text("y = 2\n")
    _commit_all(repo, "unicode names")
    req = build_request(repo, head, "HEAD")
    assert {f.path for f in req.files} == {"café.py", "my file.py"}


def test_form_feed_inside_a_line_does_not_shift_line_numbers():
    """A form feed inside an added line neither splits it nor shifts line numbers."""
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        '@@ -1,1 +1,3 @@\n keep\n+print("a\x0cb")\n+c = 1\n'
    )
    files = parse_unified_diff(diff)
    assert files[0].added == 2 and files[0].changed_lines() == [2, 3]


def test_ref_arguments_cannot_be_git_options(demo_repo):
    """A ref that looks like an option, such as ``--help``, raises ``GitError``."""
    import pytest

    from scr_agents.inputs import git as g

    repo, _base, _head = demo_repo
    with pytest.raises(g.GitError):
        g.resolve(repo, "--help")


def test_host_git_ignores_planted_hooks_and_external_diff(
    demo_repo, tmp_path, monkeypatch
):
    """Planted hooks, external diff and fsmonitor never run; checkouts lack ``.git``."""
    from scr_agents.inputs import git as g
    from scr_agents.ws3_harness.workspace import Workspace

    repo, base, head = demo_repo
    marker = tmp_path / "pwned"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    ext = tmp_path / "extdiff.sh"
    ext.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    ext.chmod(0o755)
    import subprocess

    subprocess.run(
        ["git", "-C", str(repo), "config", "diff.external", str(ext)], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.fsmonitor", str(ext)], check=True
    )

    req = build_request(repo, base, head)
    assert req.files and not marker.exists(), (
        "repo-level diff.external must not run on the host"
    )
    with Workspace(req, tmp_path / "run") as ws:
        assert (ws.head_dir / "app" / "utils.py").exists() and not (
            ws.head_dir / ".git"
        ).exists()
        assert not (ws.base_dir / ".git").exists()
    assert not marker.exists(), "repo hooks must not run on the host"
    assert not (tmp_path / "run" / "worktrees").exists()
    assert "worktrees" not in g.git(repo, "worktree", "list")


def test_exported_checkout_refuses_symlinks_that_escape(demo_repo, tmp_path):
    """A committed symlink that points outside the repo is left out of checkouts."""
    from scr_agents.ws3_harness.workspace import Workspace

    repo, base, _head = demo_repo
    (repo / "escape").symlink_to("/etc")
    _commit_all(repo, "add escaping symlink")
    req = build_request(repo, base, "HEAD")
    with Workspace(req, tmp_path / "run") as ws:
        assert (
            not (ws.head_dir / "escape").exists()
            and not (ws.head_dir / "escape").is_symlink()
        )
        assert (ws.head_dir / "app" / "utils.py").exists()
