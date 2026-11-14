"""Whole pipeline in `local` mode against a rule-based fake OpenAI-compatible server.

The fake model behaves like a tool-using reviewer: subagents call scanner_findings then
answer with findings; validators call run_tests then give a verdict. This exercises
message conversion, parallel agents, tool results and structured output over the real
HTTP path vLLM/SGLang expose.
"""

import json

from scr_agents.config import Settings
from scr_agents.models import Verdict
from scr_agents.pipeline import run_review
from tests.scr_agents.test_local_backend import FakeServer


def _tool_call(name, args):
    return {
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{name}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        },
        "finish_reason": "tool_calls",
    }


def _text(obj):
    return {
        "message": {"role": "assistant", "content": json.dumps(obj)},
        "finish_reason": "stop",
    }


def fake_model(body):
    """Answer like a tool-using model: plan, call one tool, then reply in JSON."""
    system = body["messages"][0]["content"]
    tool_names = {t["function"]["name"] for t in body.get("tools", [])}
    tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
    if "plan" in system.lower() and not tool_names:
        return _text(
            {
                "focus_files": ["app/utils.py"],
                "hypotheses": ["shell=True is reachable from user input"],
                "notes": "fake planner",
            }
        )
    if "run_tests" in tool_names:  # validator
        if not tool_msgs:
            return _tool_call("run_tests", {"ref": "head"})
        return _text(
            {
                "verdict": "confirmed",
                "notes": "fake validator reproduced it",
                "evidence": [
                    {
                        "kind": "reasoning",
                        "summary": "traced the path",
                        "command": None,
                        "output": None,
                        "exit_code": None,
                        "ref": None,
                    }
                ],
                "fix": None,
            }
        )
    if not tool_msgs:  # subagent first turn
        return _tool_call("scanner_findings", {})
    scanner_out = tool_msgs[-1]["content"]
    findings = []
    if "vulnerabilit" in system.lower() and "PY-SHELL-TRUE" in scanner_out:
        findings.append(
            {
                "title": "Command injection via shell=True",
                "category": "vulnerability",
                "severity": "high",
                "path": "app/utils.py",
                "line": 8,
                "end_line": None,
                "symbol": "run",
                "description": "run() passes a string to a shell",
                "rationale": "scanner + read",
                "cwe": ["CWE-78"],
                "confidence": 0.9,
                "suggested_fix": None,
                "reproduction": None,
                "scanner_rule": "builtin:PY-SHELL-TRUE",
            }
        )
    return _text({"findings": findings, "summary": "fake subagent"})


def test_local_mode_end_to_end(demo_repo, tmp_path):
    """A ``local`` review over HTTP confirms the planted command injection."""
    repo, base, head = demo_repo
    with FakeServer(handler=fake_model) as srv:
        settings = Settings.load(
            overrides={
                "llm": {
                    "mode": "local",
                    "local": {
                        "base_url": srv.base_url,
                        "model": "",
                        "validator_model": "qwen3-local",
                    },
                },
                "output_dir": str(tmp_path / "out"),
                "scanners": {"enabled": ["builtin"]},
            }
        )
        report, run_dir = run_review(settings, repo, base, head)
        assert report.stats["mode"] == "local" and (run_dir / "report.md").exists()
        titles = {vf.finding.title for vf in report.findings}
        assert "Command injection via shell=True" in titles
        assert all(vf.verdict == Verdict.CONFIRMED for vf in report.findings)
        # every agent went through the OpenAI-format path: system first, tools as
        # functions, tool role replies
        assert all(b["messages"][0]["role"] == "system" for b in srv.requests)
        assert any(m["role"] == "tool" for b in srv.requests for m in b["messages"])
        assert any(b.get("model") == "qwen3-local" for b in srv.requests)
        assert (
            report.stats["review_llm_calls"] >= 4
            and report.stats["validator_llm_calls"] >= 2
        )
        from scr_agents.ws5_knowledge.activity_log import ActivityLog

        mode = next(
            e
            for e in ActivityLog.read(run_dir / "activity.jsonl")
            if e["event"] == "mode"
        )
        assert mode["independent_validator"] is False, (
            "both roles resolved to the single served model"
        )
