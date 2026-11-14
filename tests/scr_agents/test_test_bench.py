import os
import sys
from pathlib import Path

from scr_agents.config import Settings
from scr_agents.inputs.request import build_request
from scr_agents.ws3_harness import test_bench
from scr_agents.ws3_harness.shell import ShellResult, ensure_tool_path
from scr_agents.ws3_harness.test_bench import missing_runner
from scr_agents.ws3_harness.workspace import Workspace


def _result(exit_code: int, stderr: str = "") -> ShellResult:
    return ShellResult(
        argv=["python3", "-m", "pytest"],
        cwd=".",
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
        duration_s=0.1,
    )


def test_ensure_tool_path_adds_the_interpreters_bin_folder(monkeypatch):
    """The interpreter's own ``bin`` folder is put first on ``PATH``, once."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    bin_dir = str(Path(sys.executable).parent)
    assert ensure_tool_path().split(os.pathsep) == [bin_dir, "/usr/bin", "/bin"]
    assert ensure_tool_path() == os.environ["PATH"]
    assert os.environ["PATH"].split(os.pathsep).count(bin_dir) == 1


def test_missing_runner_is_told_apart_from_failing_tests():
    """Only ``No module named <runner>`` counts as a missing test runner."""
    cmd = ["python3", "-m", "pytest", "-q"]
    missing = _result(1, "/usr/bin/python3: No module named pytest")
    assert "'pytest' is not installed" in (missing_runner(cmd, missing) or "")
    assert missing_runner(cmd, _result(1, "1 failed, 3 passed")) is None
    assert missing_runner(cmd, _result(0)) is None
    assert missing_runner(["make", "test"], missing) is None


def test_bench_skips_when_the_test_runner_is_not_installed(demo_repo, tmp_path):
    """A Python without pytest gives a skipped bench, not "both fail"."""
    repo, base, head = demo_repo
    settings = Settings.load()
    with Workspace(build_request(repo, base, head), tmp_path / "run") as ws:
        bench = test_bench.TestBench(settings.bench, settings.shell, ws)
        bench.shell.run = lambda *a, **k: _result(  # type: ignore[method-assign]
            1, "/usr/bin/python3: No module named pytest"
        )
        report = bench.run_both()
    assert report.base is not None and report.base.skipped
    assert "'pytest' is not installed" in report.base.reason
    assert report.delta.startswith("skipped: ")
