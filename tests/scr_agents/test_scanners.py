from pathlib import Path

from scr_agents.config import ScannerConfig
from scr_agents.inputs.git import changed_files
from scr_agents.ws2_analysis.scanners import run_scanners


def test_builtin_scanner_on_changed_lines(demo_repo):
    """The builtin rules flag the planted shell, pickle and strcpy defects."""
    repo, base, head = demo_repo
    files = changed_files(repo, base, head)
    report = run_scanners(ScannerConfig(enabled=["builtin"]), Path(repo), files)
    rules = {(f.path, f.rule) for f in report.findings}
    assert ("app/utils.py", "PY-SHELL-TRUE") in rules
    assert ("app/utils.py", "PY-PICKLE") in rules
    assert ("native/greet.c", "C-STRCPY") in rules
    assert all(f.category in ("bug", "vulnerability", "style") for f in report.findings)
    assert report.scanners_run == ["builtin"]


def test_missing_external_scanner_is_reported(demo_repo, monkeypatch):
    """A scanner not on ``PATH`` is reported missing instead of failing the run."""
    repo, base, head = demo_repo
    files = changed_files(repo, base, head)
    monkeypatch.setenv("PATH", "/nonexistent")
    report = run_scanners(ScannerConfig(enabled=["cppcheck"]), Path(repo), files)
    assert report.scanners_missing == ["cppcheck"] and not report.findings
