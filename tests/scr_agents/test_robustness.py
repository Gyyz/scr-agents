"""A failing agent degrades the review instead of killing it.

The report says what failed.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scr_agents import cli, pipeline
from scr_agents.config import Settings
from scr_agents.llm.backend import (
    BackendError,
    BackendResponse,
    OfflineBackend,
    ToolSpec,
)
from scr_agents.llm.local import LocalBackend
from scr_agents.llm.loop import ToolLoop
from scr_agents.models import Verdict
from scr_agents.ws4_guardrails.safety_filter import SafetyFilter
from scr_agents.ws5_knowledge.activity_log import ActivityLog, NullLog


def _settings(tmp_path):
    return Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "scanners": {"enabled": ["builtin"]},
        }
    )


class Crash:
    """A backend whose every ``complete`` call raises the given exception."""

    name = model = "crash"

    def __init__(self, exc: Exception):
        """Raise ``exc`` on every call."""
        self.exc = exc

    def complete(self, **kwargs):
        """Raise the configured exception."""
        raise self.exc


def _crashing(role: str, focus: str | None, exc: Exception, in_factory: bool = False):
    """Backend factory where one role (optionally one focus) fails.

    It fails inside `complete`, or, with `in_factory`, while the backend is being built
    (outside the tool loop).
    """

    def make(settings):
        def factory(r, f=None):
            if r == role and (focus is None or f == focus):
                if in_factory:
                    raise exc
                return Crash(exc)
            return OfflineBackend(r, f)

        return factory, "offline"

    return make


def test_subagent_crash_marks_the_report_incomplete_but_keeps_the_rest(
    demo_repo, tmp_path, monkeypatch
):
    """A subagent that fails to start marks the report incomplete; the rest survives."""
    repo, base, head = demo_repo
    monkeypatch.setattr(
        pipeline,
        "make_backend_factory",
        _crashing("reviewer", "style", KeyError("choices"), in_factory=True),
    )
    report, run_dir = pipeline.run_review(_settings(tmp_path), repo, base, head)
    assert report.incomplete and len(report.errors) == 1
    assert "style" in report.errors[0] and "KeyError" in report.errors[0]
    assert any(vf.verdict == Verdict.CONFIRMED for vf in report.findings), (
        "other subagents' work survives"
    )
    assert "Incomplete" in (run_dir / "report.md").read_text()
    events = ActivityLog.read(run_dir / "activity.jsonl")
    assert any(
        e["event"] == "subagent_error" and e["agent"] == "subagent:style"
        for e in events
    )
    assert "run_error" not in {e["event"] for e in events}
    assert json.loads((run_dir / "report.json").read_text())["errors"] == report.errors


def test_validator_crash_yields_unverified_findings_not_a_dead_run(
    demo_repo, tmp_path, monkeypatch
):
    """A crashing validator leaves findings unverified instead of killing the run."""
    repo, base, head = demo_repo
    monkeypatch.setattr(
        pipeline,
        "make_backend_factory",
        _crashing("validator", None, RuntimeError("gpu on fire")),
    )
    report, _ = pipeline.run_review(_settings(tmp_path), repo, base, head)
    assert report.findings and all(
        vf.verdict == Verdict.UNVERIFIED for vf in report.findings
    )
    assert all("gpu on fire" in vf.validator_notes for vf in report.findings)
    assert report.incomplete and any("validator" in e for e in report.errors)


class UsageThenCrash:
    """A backend that requests one tool call, with usage, then raises ``KeyError``."""

    name = model = "u"

    def __init__(self):
        """Start with no calls made."""
        self.n = 0

    def complete(
        self, *, system, messages, tools, output_schema=None, max_tokens=16000
    ):
        """Return a ``noop`` tool call with usage on the first call, then raise."""
        self.n += 1
        if self.n == 1:
            return BackendResponse(
                content=[{"type": "tool_use", "id": "t1", "name": "noop", "input": {}}],
                stop_reason="tool_use",
                usage={"input_tokens": 7, "output_tokens": 2},
            )
        raise KeyError("choices")


def test_loop_keeps_usage_and_reports_non_backend_exceptions():
    """A non-backend exception ends the loop as an error and keeps the usage so far."""
    loop = ToolLoop(
        UsageThenCrash(),
        [ToolSpec("noop", "n", {"type": "object"}, lambda i: "ok")],
        SafetyFilter(),
        NullLog(),
        agent="t",
        max_turns=4,
    )
    res = loop.run("s", "u", output_schema={"type": "object"})
    assert not res.ok and "KeyError" in res.error
    assert res.usage == {"input_tokens": 7, "output_tokens": 2} and res.turns == 2


class _RawServer:
    """Serves scripted raw bodies (status, content-type, bytes) in order."""

    def __init__(self, replies):
        outer = self
        self.replies = list(replies)

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return None

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                code, ctype, body = outer.replies.pop(0)
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"


def test_local_backend_turns_malformed_replies_into_backend_errors():
    """HTML or malformed JSON replies from the server raise ``BackendError``."""
    bad_choice = json.dumps({"choices": [{"message": "not an object"}]}).encode()
    with _RawServer(
        [
            (200, "text/html", b"<html>gateway hiccup</html>"),
            (200, "application/json", bad_choice),
        ]
    ) as srv:
        backend = LocalBackend(srv.base_url, "m", retries=0)
        for _ in range(2):
            with pytest.raises(BackendError):
                backend.complete(
                    system="s", messages=[{"role": "user", "content": "hi"}], tools=[]
                )


def test_review_command_exit_codes(demo_repo, tmp_path, monkeypatch):
    """``review`` exits 0, 1 on a ``--fail-on`` match and 2 for an incomplete review."""
    repo, base, head = demo_repo
    argv = [
        "review",
        "--repo",
        str(repo),
        "--base",
        base,
        "--head",
        head,
        "--mode",
        "offline",
        "--out",
        str(tmp_path / "out"),
    ]
    assert cli.main(argv) == 0
    assert cli.main([*argv, "--fail-on", "confirmed"]) == 1
    monkeypatch.setattr(
        pipeline,
        "make_backend_factory",
        _crashing("reviewer", "bug", RuntimeError("boom")),
    )
    assert cli.main(argv) == 2, "an incomplete review is a distinct exit code"


def test_backend_exception_inside_the_loop_is_also_recorded(
    demo_repo, tmp_path, monkeypatch
):
    """A crash inside the tool loop also marks the report incomplete and is logged."""
    repo, base, head = demo_repo
    monkeypatch.setattr(
        pipeline,
        "make_backend_factory",
        _crashing("reviewer", "style", KeyError("choices")),
    )
    report, run_dir = pipeline.run_review(_settings(tmp_path), repo, base, head)
    assert (
        report.incomplete
        and "style" in report.errors[0]
        and "KeyError" in report.errors[0]
    )
    events = ActivityLog.read(run_dir / "activity.jsonl")
    assert any(
        e["event"] == "llm_error" and e["agent"] == "subagent:style" for e in events
    )


def test_demo_refuses_to_wipe_a_directory_it_did_not_create(tmp_path):
    """``demo`` refuses a directory it did not create but reuses its own."""
    foreign = tmp_path / "work"
    foreign.mkdir()
    (foreign / "precious.txt").write_text("keep me")
    rc = cli.main(
        [
            "demo",
            "--dir",
            str(foreign),
            "--mode",
            "offline",
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert rc != 0 and (foreign / "precious.txt").read_text() == "keep me"
    own = tmp_path / "demo"
    assert (
        cli.main(
            [
                "demo",
                "--dir",
                str(own),
                "--mode",
                "offline",
                "--out",
                str(tmp_path / "out"),
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "demo",
                "--dir",
                str(own),
                "--mode",
                "offline",
                "--out",
                str(tmp_path / "out"),
            ]
        )
        == 0
    ), "a directory the demo created earlier is reused"


def test_cli_prints_one_line_errors_instead_of_tracebacks(tmp_path, capsys):
    """A CLI error is printed as one line on stderr, with no traceback."""
    rc = cli.main(
        [
            "review",
            "--repo",
            str(tmp_path / "nope"),
            "--base",
            "main",
            "--mode",
            "offline",
            "--out",
            str(tmp_path / "out"),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1 and "project path not found" in err and "Traceback" not in err


def test_chat_commands_survive_bad_input(demo_repo, tmp_path):
    """Chat commands with bad arguments or refs report an error and keep the session."""
    import io

    from rich.console import Console

    from scr_agents.tui.session import ChatSession

    repo, _base, _head = demo_repo
    out = io.StringIO()
    s = ChatSession(
        _settings(tmp_path),
        str(repo),
        console=Console(file=out, width=120, force_terminal=False, color_system=None),
    )
    assert s.handle("/remember don't use eval(), it's banned here") is True
    assert "don't use eval()" in (repo / ".scr_agents" / "memory.md").read_text()
    assert (
        s.handle("/log x") is True
        and s.handle("/show") is True
        and s.handle("/verdict nope accept") is True
    )
    assert s.handle("/scope nope..HEAD") is True and s.handle("/review") is True, (
        "a bad ref does not end the session"
    )
    assert "error" in out.getvalue().lower()
    s.log.close()
