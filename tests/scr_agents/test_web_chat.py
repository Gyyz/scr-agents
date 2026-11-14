"""Web chat: a conversation with the lead agent.

Its reviews and tool calls stream into one feed.
"""

import json
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scr_agents.config import Settings
from scr_agents.sessions import find_run_dir
from scr_agents.web.server import create_app

TOKEN = "t0k"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _settings(tmp_path):
    return Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "projects_dir": str(tmp_path / "out" / "projects"),
            "scanners": {"enabled": ["builtin"]},
            "web": {"token": TOKEN, "allowed_hosts": ["testserver", "localhost"]},
        }
    )


@pytest.fixture
def client(tmp_path):
    """A console client for an offline server, sending the test token."""
    with TestClient(create_app(_settings(tmp_path), token=TOKEN), headers=AUTH) as c:
        yield c


def _read_until(client, chat_id, predicate, timeout=120):
    """Poll the transcript until ``predicate(records)`` holds.

    ``follow=0`` returns what is there and closes.
    """
    deadline = time.time() + timeout
    while True:
        with client.stream("GET", f"/api/chats/{chat_id}/events?follow=0") as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            records = [
                json.loads(line[6:])
                for line in r.iter_lines()
                if line.startswith("data: ")
            ]
        records = [rec for rec in records if rec.get("event") != "replay_done"]
        if predicate(records):
            return records
        if time.time() > deadline:
            raise AssertionError(
                f"timed out; last events: {[rec.get('event') for rec in records[-8:]]}"
            )
        time.sleep(0.2)


def _replies(records):
    return [
        r
        for r in records
        if r.get("event") == "message"
        and r.get("role") == "assistant"
        and not r.get("intro")
    ]


def test_chat_drives_a_review_and_answers(client, tmp_path):
    """A chat message starts a review; its events and the answer share one feed."""
    res = client.post(
        "/api/chats/new",
        json={"demo": True, "text": "check the last commit for security problems"},
    )
    assert res.status_code == 201, res.text
    chat = res.json()
    cid = chat["chat_id"]
    assert chat["title"].startswith("scr-agents-demo-") and chat["live"] is True

    records = _read_until(client, cid, lambda rs: bool(_replies(rs)))
    kinds = [r["event"] for r in records]
    # the request comes first, then the repository is prepared, then a short intro
    # (no "what should I check?": the person already said) and the answer
    assert records[0]["event"] == "message" and records[0]["role"] == "user"
    assert records[0]["text"] == "check the last commit for security problems"
    intro = next(r for r in records if r.get("intro"))
    assert (
        intro["text"].startswith("Attached **scr-agents-demo-")
        and "What should I check?" not in intro["text"]
    )
    assert (
        kinds.index("repo_ready")
        < records.index(intro)
        < records.index(_replies(records)[0])
    )
    # the lead agent's own work is in the feed: its model turns and the review tool call
    assert "llm_start" in kinds and any(
        r.get("event") == "tool_start" and r.get("tool") == "start_review"
        for r in records
    )
    started = next(r for r in records if r["event"] == "run_started")
    done = next(r for r in records if r["event"] == "run_done")
    assert (
        started["run"] == done["run"]
        and done["status"] == "finished"
        and done["counts"]["confirmed"] >= 1
    )
    assert "confirmed" in _replies(records)[0]["text"]
    assert (
        kinds.index("run_started")
        < kinds.index("run_done")
        < kinds.index("message", kinds.index("run_done"))
    )

    # the review belongs to the chat's session, and its own event stream works as before
    run = client.get(f"/api/runs/{started['run']}").json()
    assert run["session_id"] == cid and f"/sessions/{cid}/" in run["run_dir"]
    listed = client.get("/api/chats").json()
    assert listed[0]["chat_id"] == cid and listed[0]["runs"] == [started["run"]]
    assert listed[0]["first_message"] == "check the last commit for security problems"
    deadline = time.time() + 10
    while client.get(f"/api/chats/{cid}").json()["busy"] and time.time() < deadline:
        time.sleep(0.1)
    assert client.get(f"/api/chats/{cid}").json()["busy"] is False


def test_chat_replays_and_resumes_after_restart(tmp_path):
    """After a restart the chat replays from disk and a new message resumes it."""
    settings = _settings(tmp_path)
    with TestClient(create_app(settings, token=TOKEN), headers=AUTH) as c1:
        cid = c1.post(
            "/api/chats/new", json={"demo": True, "text": "review the last commit"}
        ).json()["chat_id"]
        first = _read_until(c1, cid, lambda rs: bool(_replies(rs)))
    with TestClient(create_app(settings, token=TOKEN), headers=AUTH) as c2:
        # not loaded: the transcript comes from disk and the stream ends
        with c2.stream("GET", f"/api/chats/{cid}/events") as r:
            replay = [
                json.loads(line[6:])
                for line in r.iter_lines()
                if line.startswith("data: ")
            ]
        assert replay[-1]["event"] == "replay_done"
        assert [r["seq"] for r in replay[:-1]] == [
            r["seq"] for r in first[: len(replay) - 1]
        ]
        assert c2.get(f"/api/chats/{cid}").json()["live"] is False
        # a new message restores the chat: numbering continues, the lead remembers
        # the conversation
        assert (
            c2.post(
                f"/api/chats/{cid}/messages", json={"text": "review the whole project"}
            ).status_code
            == 202
        )
        chat = c2.app.state.chats.get(cid, restore=False)
        assert chat is not None and chat.lead.history[0] == (
            "user",
            "review the last commit",
        )
        records = _read_until(c2, cid, lambda rs: len(_replies(rs)) >= 2)
        seqs = [r["seq"] for r in records if "seq" in r]
        assert seqs == sorted(set(seqs)), (
            "sequence numbers stay unique across the restart"
        )
        assert len([r for r in records if r["event"] == "run_done"]) == 2


def test_chat_rejects_local_paths_and_empty_messages(client, tmp_path):
    """Chats refuse local paths, empty messages, unknown ids and missing tokens."""
    res = client.post("/api/chats/new", json={"repo": str(tmp_path)})
    assert res.status_code == 400 and "local paths" in res.json()["error"]
    assert client.post("/api/chats/new", json={}).status_code == 400
    cid = client.post("/api/chats/new", json={"demo": True}).json()["chat_id"]
    assert (
        client.post(f"/api/chats/{cid}/messages", json={"text": "  "}).status_code
        == 400
    )
    assert (
        client.post("/api/chats/session-nope/messages", json={"text": "hi"}).status_code
        == 404
    )
    assert client.get("/api/chats/session-nope").status_code == 404
    unauth = TestClient(client.app)
    assert unauth.get("/api/chats").status_code == 401
    assert Path(client.get(f"/api/chats/{cid}").json()["repo"]).exists()


def test_chat_started_with_openflow_reviews_with_openflow(client):
    """The welcome form's orchestrator choice runs the chat's reviews."""
    assert client.get("/api/config").json()["orchestrators"] == [
        "deterministic",
        "openflow",
    ]
    bad = client.post("/api/chats/new", json={"demo": True, "orchestrator": "magic"})
    assert bad.status_code == 400 and "orchestrator" in bad.json()["error"]

    chat = client.post(
        "/api/chats/new",
        json={
            "demo": True,
            "text": "review the last commit",
            "orchestrator": "openflow",
        },
    ).json()
    assert chat["orchestrator"] == "openflow"
    records = _read_until(
        client,
        chat["chat_id"],
        lambda recs: any(r.get("event") == "run_done" for r in recs),
    )
    started = next(r for r in records if r.get("event") == "run_started")
    assert started["orchestrator"] == "openflow"
    run_dir = find_run_dir(client.app.state.manager.settings, started["run"])
    report = json.loads((run_dir / "report.json").read_text())
    assert report["stats"]["orchestrator"] == "openflow"
    assert "openflow_orchestrator" in report["stats"]["prompts_used"]
