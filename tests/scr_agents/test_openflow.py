import json
from typing import Any

from scr_agents.cli import _settings, build_parser
from scr_agents.config import Settings
from scr_agents.llm.backend import BackendError, BackendResponse, OfflineBackend
from scr_agents.models import Verdict
from scr_agents.pipeline import run_review
from scr_agents.ws1_review.openflow import OpenFlowAgent
from scr_agents.ws5_knowledge.activity_log import ActivityLog


class ScriptedOrchestrator(OfflineBackend):
    """An orchestrator model that runs one reviewer and then answers as told.

    It runs the vulnerabilities reviewer, lists the candidates, validates the first
    ``validate_first`` of them and answers with ``answer(ids)``.
    """

    def __init__(self, answer: Any, validate_first: int = 1):
        """Keep the answer to give and how many candidates to validate."""
        super().__init__("orchestrator")
        self.answer = answer
        self.validate_first = validate_first

    def complete(self, *, messages, tools, **_: Any) -> BackendResponse:
        """Return the next scripted step for this conversation."""
        called = self._called(messages)
        if "run_reviewer" not in called:
            return self._tool_use("run_reviewer", {"agent": "vulnerabilities"})
        if "list_candidates" not in called:
            return self._tool_use("list_candidates", {})
        ids = [
            c["id"]
            for c in json.loads(self._strip_untrusted(called["list_candidates"]))
        ]
        if self.validate_first and "validate" not in called:
            return self._tool_use("validate", {"ids": ids[: self.validate_first]})
        return self._final(self.answer(ids))


class FailingOrchestrator(OfflineBackend):
    """An orchestrator model whose server is down."""

    def __init__(self) -> None:
        """Set up as the orchestrator role."""
        super().__init__("orchestrator")

    def complete(self, **_: Any) -> BackendResponse:
        """Fail every call."""
        raise BackendError("connection refused")


def _flow(tool_context, registry, tmp_path, orchestrator):
    def factory(role: str, focus: str | None = None):
        return orchestrator if role == "orchestrator" else OfflineBackend(role, focus)

    log = ActivityLog(tmp_path / "activity.jsonl", "run-x", [])
    return OpenFlowAgent(tool_context.settings, registry, log, factory), log


def test_offline_openflow_matches_the_deterministic_flow(demo_repo, tmp_path):
    """Offline, both orchestrators confirm the same planted defects."""
    repo, base, head = demo_repo
    found = {}
    for orchestrator in ("deterministic", "openflow"):
        settings = Settings.load(
            overrides={
                "llm": {"mode": "offline"},
                "output_dir": str(tmp_path / orchestrator),
                "scanners": {"enabled": ["builtin"]},
                "review": {"orchestrator": orchestrator},
            }
        )
        report, run_dir = run_review(settings, repo, base, head)
        assert not report.errors and report.stats["orchestrator"] == orchestrator
        found[orchestrator] = {(vf.finding.title, vf.verdict) for vf in report.findings}
    assert found["openflow"] == found["deterministic"]
    assert any(v == Verdict.CONFIRMED for _, v in found["openflow"])
    assert "openflow_orchestrator" in report.stats["prompts_used"]
    assert "review_planner" not in report.stats["prompts_used"]
    assert len(report.stats["openflow"]["reviewer_runs"]) == 3
    events = {e["event"] for e in ActivityLog.read(run_dir / "activity.jsonl")}
    assert {"openflow_start", "openflow_done", "subagent_done", "verdict"} <= events


def test_orchestrator_cannot_drop_a_confirmed_finding(tool_context, registry, tmp_path):
    """Dropping everything keeps the confirmed finding; the rest are recorded."""
    orch = ScriptedOrchestrator(
        lambda ids: {
            "report": [],
            "dropped": [{"id": i, "reason": "the code says it is safe"} for i in ids],
            "notes": "",
        }
    )
    flow, _ = _flow(tool_context, registry, tmp_path, orch)
    _, findings = flow.run(tool_context)
    assert [vf.verdict for vf in findings] == [Verdict.CONFIRMED]
    dropped = flow.stats()["openflow"]["dropped"]
    assert dropped and all(d["reason"] == "the code says it is safe" for d in dropped)
    assert findings[0].finding.id not in {d["id"] for d in dropped}


def test_unmentioned_candidates_are_reported_and_validated(
    tool_context, registry, tmp_path
):
    """Candidates the answer leaves out are reported and validated at the end."""
    orch = ScriptedOrchestrator(
        lambda ids: {"report": [], "dropped": [], "notes": "n"}, validate_first=0
    )
    flow, _ = _flow(tool_context, registry, tmp_path, orch)
    plan, findings = flow.run(tool_context)
    assert findings and all(vf.verdict == Verdict.CONFIRMED for vf in findings)
    assert plan.notes == "n"


def test_validation_budget_holds_for_the_whole_round(tool_context, registry, tmp_path):
    """``llm.max_findings_to_validate`` caps validations across all calls."""
    tool_context.settings.llm.max_findings_to_validate = 1
    orch = ScriptedOrchestrator(
        lambda ids: {"report": ids, "dropped": [], "notes": ""}, validate_first=99
    )
    flow, _ = _flow(tool_context, registry, tmp_path, orch)
    _, findings = flow.run(tool_context)
    assert len(findings) > 1
    assert [vf.verdict for vf in findings].count(Verdict.CONFIRMED) == 1
    assert all(
        "budget" in vf.validator_notes
        for vf in findings
        if vf.verdict == Verdict.UNVERIFIED
    )


def test_failing_orchestrator_degrades_to_the_deterministic_flow(
    tool_context, registry, tmp_path
):
    """When the orchestrator model fails, the round runs the fixed flow instead."""
    flow, _ = _flow(tool_context, registry, tmp_path, FailingOrchestrator())
    _, findings = flow.run(tool_context)
    assert any(e.startswith("orchestrator:") for e in flow.errors)
    assert any(vf.verdict == Verdict.CONFIRMED for vf in findings)
    events = ActivityLog.read(tmp_path / "activity.jsonl")
    assert any(e["event"] == "openflow_fallback" for e in events)


def test_orchestrator_is_chosen_by_flag_env_and_config(monkeypatch):
    """``--orchestrator`` beats ``SCR_AGENTS_ORCHESTRATOR``, which beats the default."""
    assert Settings.load().review.orchestrator == "deterministic"
    monkeypatch.setenv("SCR_AGENTS_ORCHESTRATOR", "openflow")
    assert Settings.load().review.orchestrator == "openflow"
    args = build_parser().parse_args(
        ["review", "--base", "HEAD~1", "--orchestrator", "deterministic"]
    )
    assert _settings(args).review.orchestrator == "deterministic"
