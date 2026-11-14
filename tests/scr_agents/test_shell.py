import os
import time
from pathlib import Path

import pytest

from scr_agents.config import ShellConfig
from scr_agents.ws3_harness.shell import SandboxedShell, ShellPolicyError

BINS = ["ls", "python3", "node", "find", "git", "rg", "cat", "cc", "make"]


def _shell(root: Path, **kw) -> SandboxedShell:
    return SandboxedShell(ShellConfig(allowed_binaries=BINS, **kw), root)


def _script(root: Path, name: str, body: str) -> str:
    (root / name).write_text(body)
    return name


def test_allowlist_and_cwd_confinement(tmp_path):
    """Only allowlisted binaries run, and only from a directory inside the root."""
    (tmp_path / "sub").mkdir()
    sh = _shell(tmp_path)
    res = sh.run(["ls"], cwd=tmp_path / "sub")
    assert res.exit_code == 0 and res.error is None
    with pytest.raises(ShellPolicyError):
        sh.run(["bash", "-c", "echo hi"])
    with pytest.raises(ShellPolicyError):
        sh.run(["ls"], cwd=tmp_path.parent)
    with pytest.raises(ShellPolicyError):
        sh.run(["./evil.sh"])


def test_timeout_and_env_scrub(tmp_path, monkeypatch):
    """API keys are scrubbed from the environment and a slow command times out."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    sh = _shell(tmp_path, timeout_s=1)
    env = _script(
        tmp_path, "env.py", "import os; print(os.environ.get('ANTHROPIC_API_KEY'))"
    )
    assert sh.run(["python3", env]).stdout.strip() == "None"
    slow = _script(tmp_path, "slow.py", "import time; time.sleep(5)")
    res = sh.run(["python3", slow], timeout=1)
    assert res.timed_out and res.exit_code is None


@pytest.mark.parametrize(
    "argv",
    [
        ["python3", "-c", "print(1)"],
        ["python3", "-m", "http.server"],
        ["node", "-e", "1"],
        ["node", "--eval", "1"],
        ["find", ".", "-exec", "rm", "{}", ";"],
        ["find", ".", "-delete"],
        ["git", "-c", "alias.x=!id", "x"],
        ["git", "push", "origin", "main"],
        ["git", "-C", "/", "log"],
        ["rg", "--pre", "/bin/sh", "x"],
    ],
)
def test_shell_equivalent_arguments_are_denied(tmp_path, argv):
    """Arguments that make a policed binary as powerful as a shell are refused."""
    with pytest.raises(ShellPolicyError):
        _shell(tmp_path).run(argv)


def test_safe_forms_of_policed_binaries_still_run(tmp_path):
    """Harmless forms of the argument-policed binaries are still allowed to run."""
    sh = _shell(tmp_path)
    mod = _script(tmp_path, "mod.py", "x = 1\n")
    assert sh.run(["python3", "-m", "py_compile", mod]).exit_code == 0
    assert sh.run(["python3", mod]).exit_code == 0
    assert sh.run(["find", ".", "-name", "*.py"]).exit_code == 0
    assert sh.run(["git", "--version"]).exit_code == 0  # a plain option, no subcommand


@pytest.mark.parametrize(
    "argv",
    [
        ["cat", "/etc/passwd"],
        ["cat", "../outside.txt"],
        ["cat", "sub/../../outside.txt"],
        ["cc", "-o", "/tmp/evil", "x.c"],
        ["cat", "~/.ssh/id_rsa"],
        ["cc", "--output=/tmp/evil", "x.c"],
    ],
)
def test_paths_outside_the_root_are_denied(tmp_path, argv):
    """Absolute, home-relative and ``..`` paths that leave the root are refused."""
    (tmp_path / "sub").mkdir()
    with pytest.raises(ShellPolicyError):
        _shell(tmp_path).run(argv)


def test_paths_inside_the_root_and_system_prefixes_are_allowed(tmp_path):
    """Paths that stay inside the root, and allowed system prefixes, pass the check."""
    sh = _shell(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("hi")
    assert sh.run(["cat", "sub/a.txt"]).stdout == "hi"
    assert sh.run(["cat", str(tmp_path / "sub" / "a.txt")]).stdout == "hi"
    (tmp_path / "a.txt").write_text("top")
    assert (
        sh.run(["cat", "../a.txt"], cwd=tmp_path / "sub").stdout == "top"
    )  # .. that stays inside the root
    sh._check(["cc", "-I/usr/include", "x.c"], None)  # system prefix is fine
    sh._check(["cat", "/dev/null"], None)


def test_home_and_git_config_are_isolated(tmp_path, monkeypatch):
    """Commands get a throwaway HOME inside the root and no global git config."""
    monkeypatch.setenv("HOME", "/Users/someone")
    sh = _shell(tmp_path)
    env = _script(
        tmp_path,
        "env.py",
        (
            "import os; print(os.environ['HOME']); "
            "print(os.environ.get('GIT_CONFIG_GLOBAL'))"
        ),
    )
    home, git_cfg = sh.run(["python3", env]).stdout.split()
    assert (
        Path(home).resolve().is_relative_to(tmp_path.resolve()) and Path(home).is_dir()
    )
    assert git_cfg == "/dev/null"


def test_timeout_is_clamped_and_whole_process_group_is_killed(tmp_path):
    """A too-long timeout is clamped and a timeout also kills the grandchild process."""
    sh = _shell(tmp_path, timeout_s=1)
    spawn = _script(
        tmp_path,
        "spawn.py",
        (
            "import subprocess, sys, time\n"
            "c = subprocess.Popen("
            "[sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "open('child.pid', 'w').write(str(c.pid))\n"
            "c.wait()\n"
        ),
    )
    t0 = time.monotonic()
    res = sh.run(
        ["python3", spawn], timeout=600
    )  # asks for more than the configured limit
    assert res.timed_out and time.monotonic() - t0 < 5
    pid = int((tmp_path / "child.pid").read_text())
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("grandchild survived the timeout")


def test_output_is_capped(tmp_path):
    """Output beyond ``max_output_chars`` is cut off and flagged as truncated."""
    sh = _shell(tmp_path, max_output_chars=100)
    big = _script(tmp_path, "big.py", "print('x' * 10000)")
    res = sh.run(["python3", big])
    assert res.truncated and len(res.stdout) == 100
