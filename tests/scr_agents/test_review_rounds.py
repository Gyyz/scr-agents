import json
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scr_agents.config import Settings
from scr_agents.llm.backend import OfflineBackend
from scr_agents.models import (
    Category,
    EvidenceReport,
    Finding,
    Location,
    ReviewRequest,
    ValidatedFinding,
    Verdict,
)
from scr_agents.pipeline import run_auto_rounds, run_next_round, run_review
from scr_agents.web.server import create_app
from scr_agents.ws1_review.lead import LeadAgent
from scr_agents.ws1_review.rounds import (
    FALSE,
    PENDING,
    REAL,
    RoundError,
    drop_settled,
    labels,
    prepare_round,
    round_section,
    round_status,
    run_status,
    settle,
)
from scr_agents.ws4_guardrails.safety_filter import SafetyFilter
from scr_agents.ws5_knowledge.activity_log import ActivityLog, NullLog
from scr_agents.ws5_knowledge.human_review import record_verdict

TOKEN = "test-token-123"


def _settings(tmp_path: Path, **review: int) -> Settings:
    return Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "projects_dir": str(tmp_path / "projects"),
            "scanners": {"enabled": ["builtin"]},
            "review": review,
            "web": {"token": TOKEN, "allowed_hosts": ["testserver"]},
        }
    )


def _vf(
    verdict: Verdict, fid: str = "F-1", line: int = 10, **kw: object
) -> ValidatedFinding:
    return ValidatedFinding(
        finding=Finding(
            id=fid, title=f"problem {fid}", location=Location(path="a.c", line=line)
        ),
        verdict=verdict,
        **kw,
    )


def _report(findings: list[ValidatedFinding], **kw: object) -> EvidenceReport:
    request = ReviewRequest(
        repo_path="/r", base_ref="main", head_ref="dev", base="a" * 40, head="b" * 40
    )
    return EvidenceReport(run_id="run-1", request=request, findings=findings, **kw)


# ------------------------------------------------------------------- settling rules


@pytest.mark.parametrize(
    ("verdict", "human", "auto", "state"),
    [
        (Verdict.CONFIRMED, "accept", False, REAL),
        (Verdict.CONFIRMED, "reject", False, PENDING),
        (Verdict.REJECTED, "reject", False, FALSE),
        (Verdict.REJECTED, "accept", False, PENDING),
        (Verdict.UNVERIFIED, "accept", False, REAL),
        (Verdict.UNVERIFIED, "reject", False, PENDING),
        (Verdict.CONFIRMED, None, False, PENDING),
        (Verdict.CONFIRMED, None, True, REAL),
        (Verdict.REJECTED, None, True, FALSE),
        (Verdict.UNVERIFIED, None, True, PENDING),
    ],
)
def test_settle_follows_the_agreed_table(verdict, human, auto, state):
    """A finding is settled only when the person agrees with the validator."""
    rec = {"verdict": human} if human else None
    assert settle(_vf(verdict), rec, auto) == state


def test_labels_name_reports_a_and_b_per_round():
    """Round 1 writes the Review and Validator Reports; later rounds name the round."""
    assert labels(1) == ("Review Report", "Validator Report")
    assert labels(2) == ("Review Report (round 2)", "Validator Report (round 2)")
    assert labels(3) == ("Review Report (round 3)", "Validator Report (round 3)")


def test_round_status_asks_when_the_validator_disagrees():
    """A rejected or unverified finding makes an interactive round ask the user."""
    report = _report(
        [_vf(Verdict.CONFIRMED, "F-1"), _vf(Verdict.UNVERIFIED, "F-2")], max_rounds=3
    )
    st = round_status(report, [{"finding_id": "F-1", "verdict": "accept"}])
    assert st.settled_real == ["F-1"] and st.pending == ["F-2"]
    assert st.awaiting_verdict == ["F-2"] and not st.validator_passed
    assert st.can_continue and st.ask_user and not st.passed


def test_round_status_respects_the_round_limit():
    """At the round limit nothing more is offered, even with pending findings."""
    report = _report([_vf(Verdict.UNVERIFIED)], round=3, max_rounds=3)
    st = round_status(report, [])
    assert st.pending == ["F-1"] and not st.can_continue and not st.ask_user
    assert round_status(report, [], max_rounds=4).can_continue


def test_carried_findings_stay_settled():
    """Findings carried from an earlier round count as settled in later ones."""
    carried = _vf(
        Verdict.UNVERIFIED, "F-1", carried_from="run-0", human_verdict="accept"
    )
    st = round_status(_report([carried], round=2, max_rounds=3), [])
    assert st.settled_real == ["F-1"] and st.passed


def test_drop_settled_skips_repeats_near_a_settled_finding():
    """A new report of the same kind within a few lines of a settled one is left out."""
    settled = [_vf(Verdict.CONFIRMED, "F-1", line=10)]
    near = Finding(
        id="F-2", title="other words", location=Location(path="a.c", line=12)
    )
    far = Finding(id="F-3", title="other", location=Location(path="a.c", line=40))
    style = Finding(
        id="F-4",
        title="naming",
        category=Category.STYLE,
        location=Location(path="a.c", line=11),
    )
    kept, dropped = drop_settled([near, far, style], settled)
    assert dropped == ["F-2"] and [f.id for f in kept] == ["F-3", "F-4"]


# ------------------------------------------------------------------- whole rounds


def test_next_round_carries_settled_and_reviews_the_rest(demo_repo, tmp_path):
    """Round 2 carries the accepted finding and reviews the others again.

    Its planner and subagents get the earlier round's findings wrapped as untrusted
    content, and it writes the round 2 Review Report and Validator Report.
    """
    repo, base, head = demo_repo
    settings = _settings(tmp_path)
    r1, d1 = run_review(settings, repo, base, head)
    assert r1.round == 1 and (d1 / "report_a.html").is_file()
    assert "Validator Report" in (d1 / "report_b.html").read_text()
    first, second, *rest = r1.findings
    record_verdict(d1, first.finding.id, "accept")
    record_verdict(d1, second.finding.id, "reject", "not reachable")
    st = run_status(d1)
    assert first.finding.id in st.settled_real and second.finding.id in st.disputed

    r2, d2 = run_next_round(settings, d1)
    assert r2.round == 2 and r2.previous_run_id == r1.run_id
    assert r2.request.session_id == r1.request.session_id and d2.parent == d1.parent
    carried = [v for v in r2.findings if v.carried_from]
    assert [v.finding.id for v in carried] == [first.finding.id]
    assert carried[0].human_verdict == "accept"
    assert set(r2.replaced) == {second.finding.id, *(v.finding.id for v in rest)}
    assert all(v.round == 2 for v in r2.findings if not v.carried_from)
    assert r2.plan.focus_files[0] == second.finding.location.path

    events = ActivityLog.read(d2 / "activity.jsonl")
    rnd = next(e for e in events if e["event"] == "round")
    assert rnd["round"] == 2 and rnd["carried"] == [first.finding.id]
    assert any(e["event"] == "round_status" for e in events)
    planner = json.loads(
        next((d2 / "trace").glob("*-review_agent-turn1.json")).read_text()
    )
    prompt = json.dumps(planner["request"]["messages"])
    assert '<untrusted source=\\"previous_round\\"' in prompt
    assert "Review round 2 of 3" in prompt and "not reachable" in prompt

    page_a = (d2 / "report_a.html").read_text()
    assert "Review Report (round 2)" in page_a and first.finding.id not in page_a
    page_b = (d2 / "report_b.html").read_text()
    assert (
        "Validator Report (round 2)" in page_b and "Settled in earlier rounds" in page_b
    )
    assert "settled in round 1" in (d2 / "report.html").read_text()


def test_round_limit_stops_the_next_round(demo_repo, tmp_path):
    """With ``review.max_rounds: 1`` no second round can start."""
    repo, base, head = demo_repo
    settings = _settings(tmp_path, max_rounds=1)
    _, d1 = run_review(settings, repo, base, head)
    with pytest.raises(RoundError, match="round limit"):
        prepare_round(d1, settings.review.max_rounds)


def test_auto_rounds_settle_by_the_validator(demo_repo, tmp_path):
    """An automatic run settles confirmed findings itself and stops when it passed."""
    repo, base, head = demo_repo
    settings = _settings(tmp_path)
    report, run_dir = run_review(settings, repo, base, head, auto_rounds=True)
    report, run_dir = run_auto_rounds(settings, report, run_dir)
    st = run_status(run_dir)
    assert report.auto_rounds and st.auto
    assert st.passed or not st.can_continue
    if st.passed:
        with pytest.raises(RoundError, match="passed"):
            run_next_round(settings, run_dir)


def test_round_section_wraps_earlier_findings(demo_repo, tmp_path):
    """The earlier round's findings reach prompts inside an untrusted wrapper."""
    repo, base, head = demo_repo
    settings = _settings(tmp_path)
    _, d1 = run_review(settings, repo, base, head)
    prior = prepare_round(d1, 3)
    text = round_section(prior, SafetyFilter())
    assert "Review round 2 of 3" in text and "Review Report (round 2)" in text
    assert '<untrusted source="previous_round"' in text
    assert round_section(None, SafetyFilter()) == ""


# ------------------------------------------------------------------- web and chat


def test_web_round_pages_and_next_round(demo_repo, tmp_path):
    """The console serves both round reports, the round status, and starts round 2."""
    repo, base, head = demo_repo
    settings = _settings(tmp_path)
    r1, _ = run_review(settings, repo, base, head)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(create_app(settings), headers=auth) as c:
        base_url = f"/api/runs/{r1.run_id}"
        assert "Review Report" in c.get(f"{base_url}/report_a.html").text
        page_b = c.get(f"{base_url}/report_b.html")
        assert page_b.status_code == 200 and "content-security-policy" in page_b.headers
        st = c.get(f"{base_url}/round").json()
        assert st["round"] == 1 and st["can_continue"]
        r = c.post(f"{base_url}/next-round", json={})
        assert r.status_code == 201, r.text
        new = r.json()["run_id"]
        for _ in range(400):
            if c.get(f"/api/runs/{new}").json()["status"] != "running":
                break
            time.sleep(0.3)
        assert c.get(f"/api/runs/{new}/round").json()["round"] == 2
        assert c.get("/api/runs/bad id/round").status_code in (400, 404)


class _RoundSession:
    """A chat session with one finished review whose next round is recorded."""

    def __init__(self, report: EvidenceReport):
        """Hold ``report`` as the last review; no run directory."""
        self.repo, self.base, self.head, self.scope = Path("."), "a", "b", "range"
        self.last_report, self.last_run_dir, self.memory = report, None, None
        self.next_rounds = 0
        self.skills = None

    def project_info(self):
        """Return minimal project facts."""
        return {"path": "."}

    def run_review(self, **kwargs):
        """Fail the test: the request asked for the next round."""
        raise AssertionError("start_review was not expected")

    def run_next_round(self):
        """Count the call and return the next round's report."""
        self.next_rounds += 1
        self.last_report = self.last_report.model_copy(update={"round": 2})
        return self.last_report


def test_offline_lead_starts_the_next_round_when_asked(registry):
    """Asked for another round, the offline lead calls ``start_next_round``."""
    session = _RoundSession(_report([_vf(Verdict.UNVERIFIED)], max_rounds=3))
    agent = LeadAgent(
        session, OfflineBackend("lead"), registry, SafetyFilter(), NullLog()
    )
    reply, _ = agent.ask("Yes, start the next round please")
    assert session.next_rounds == 1 and "Round 2 of 3" in reply


def test_offline_lead_says_why_a_round_cannot_start(registry):
    """Without a review to continue, the reply is the reason, not an empty summary."""
    session = _RoundSession(_report([]))
    session.last_report = None
    agent = LeadAgent(
        session, OfflineBackend("lead"), registry, SafetyFilter(), NullLog()
    )
    reply, _ = agent.ask("start the next round")
    assert reply.startswith("Could not start: no review has run yet")
    assert "finished" not in reply and session.next_rounds == 0


def test_lead_context_names_the_interface(registry):
    """The lead agent is told whether the person uses the terminal or the console."""
    session = _RoundSession(_report([]))
    agent = LeadAgent(
        session, OfflineBackend("lead"), registry, SafetyFilter(), NullLog()
    )
    assert "Interface: terminal" in agent._context()
    session.interface = "web console"
    assert "Interface: web console" in agent._context()


def test_web_chat_next_round_keeps_the_refs_of_the_first(tmp_path):
    """Round 2 started in a chat is announced with the first round's refs."""
    settings = _settings(tmp_path)
    auth = {"Authorization": f"Bearer {TOKEN}"}

    def started(c: TestClient, cid: str, n: int) -> list[dict]:
        for _ in range(600):
            with c.stream("GET", f"/api/chats/{cid}/events?follow=0") as r:
                recs = [
                    json.loads(line[6:])
                    for line in r.iter_lines()
                    if line.startswith("data: ")
                ]
            runs = [x for x in recs if x.get("event") == "run_started"]
            done = [x for x in recs if x.get("event") == "run_done"]
            if len(runs) >= n and len(done) >= n:
                return runs
            time.sleep(0.2)
        raise AssertionError("the chat did not start the review")

    with TestClient(create_app(settings), headers=auth) as c:
        res = c.post("/api/chats/new", json={"demo": True, "text": "check it"})
        assert res.status_code == 201, res.text
        cid = res.json()["chat_id"]
        (first,) = started(c, cid, 1)
        for _ in range(100):
            if not c.get(f"/api/chats/{cid}").json()["busy"]:
                break
            time.sleep(0.1)
        sent = c.post(f"/api/chats/{cid}/messages", json={"text": "next round please"})
        assert sent.status_code in (200, 201, 202), sent.text
        _, second = started(c, cid, 2)
    assert second["round"] == 2 and second["previous_run"] == first["run"]
    assert (second["base"], second["head"]) == (first["base"], first["head"])
    assert len(second["base"]) < 40
