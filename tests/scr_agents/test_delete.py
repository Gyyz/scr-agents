"""Deleting history: conversations and runs, from the console API and the CLI."""

import json
import subprocess
import sys
import time

import pytest
from starlette.testclient import TestClient

from scr_agents.config import Settings
from scr_agents.sessions import (
    DeleteError,
    delete_run,
    delete_session,
    find_run_dir,
    session_dir,
)
from scr_agents.web.server import create_app

TOKEN = "del-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _settings(tmp_path):
    return Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "projects_dir": str(tmp_path / "out" / "projects"),
            "scanners": {"enabled": ["builtin"]},
            "web": {"token": TOKEN, "allowed_hosts": ["testserver"]},
        }
    )


def _delete(client, path, **headers):
    return client.request(
        "DELETE",
        path,
        content="{}",
        headers={"content-type": "application/json", **headers},
    )


def _wait_reply(client, cid, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with client.stream("GET", f"/api/chats/{cid}/events?follow=0") as r:
            recs = [
                json.loads(line[6:])
                for line in r.iter_lines()
                if line.startswith("data: ")
            ]
        if (
            any(
                x.get("event") == "message"
                and x.get("role") == "assistant"
                and not x.get("intro")
                for x in recs
            )
            and not client.get(f"/api/chats/{cid}").json()["busy"]
        ):
            return recs
        time.sleep(0.2)
    raise AssertionError("no reply")


def test_delete_a_conversation_and_its_reviews(tmp_path):
    """Deleting a chat removes its session and runs, but not while it is busy."""
    settings = _settings(tmp_path)
    with TestClient(create_app(settings, token=TOKEN), headers=AUTH) as c:
        cid = c.post(
            "/api/chats/new", json={"demo": True, "text": "review the last commit"}
        ).json()["chat_id"]
        recs = _wait_reply(c, cid)
        run_id = next(r["run"] for r in recs if r["event"] == "run_started")
        assert (
            session_dir(settings, cid).is_dir()
            and find_run_dir(settings, run_id) is not None
        )

        chat = c.app.state.chats.get(cid, restore=False)
        chat.busy = True  # the lead agent is still answering
        res = _delete(c, f"/api/chats/{cid}")
        assert res.status_code == 409 and "still working" in res.json()["error"]
        chat.busy = False

        res = _delete(c, f"/api/chats/{cid}")
        assert res.status_code == 200, res.text
        assert res.json()["runs"] == [run_id]
        assert (
            not session_dir(settings, cid).exists()
            and find_run_dir(settings, run_id) is None
        )
        assert all(x["chat_id"] != cid for x in c.get("/api/chats").json())
        assert c.get(f"/api/chats/{cid}").status_code == 404
        assert c.get(f"/api/runs/{run_id}").status_code == 404
        assert (
            c.post(f"/api/chats/{cid}/messages", json={"text": "hi"}).status_code == 404
        )
        assert _delete(c, f"/api/chats/{cid}").status_code == 404  # already gone


def test_delete_a_past_run_and_refuse_a_running_one(tmp_path):
    """A finished run is deleted with its emptied session; a running one gets 409."""
    settings = _settings(tmp_path)
    with TestClient(create_app(settings, token=TOKEN), headers=AUTH) as c:
        run_id = c.post("/api/demo", json={}).json()["run_id"]
        deadline = time.time() + 120
        while c.get(f"/api/runs/{run_id}").json()["status"] == "running":
            assert time.time() < deadline
            time.sleep(0.2)
        live = c.app.state.manager.get(run_id)
        live.status = "running"
        assert _delete(c, f"/api/runs/{run_id}").status_code == 409
        live.status = "finished"
        sid = live.request.session_id
        res = _delete(c, f"/api/runs/{run_id}")
        assert res.status_code == 200 and res.json()["removed_empty_session"] == sid
        assert (
            find_run_dir(settings, run_id) is None
            and not session_dir(settings, sid).exists()
        )
        assert all(r["run_id"] != run_id for r in c.get("/api/runs").json())
        # a new review still works: the server's session is recreated on demand
        again = c.post("/api/demo", json={}).json()["run_id"]
        assert c.get(f"/api/runs/{again}").status_code == 200


def test_delete_rejects_bad_ids_cross_site_and_anonymous_requests(tmp_path):
    """Unknown or traversing ids, other origins and missing tokens are refused."""
    with TestClient(create_app(_settings(tmp_path), token=TOKEN), headers=AUTH) as c:
        assert _delete(c, "/api/runs/run-nope").status_code == 404
        assert _delete(c, "/api/chats/session-nope").status_code == 404
        assert _delete(c, "/api/runs/..%2F..").status_code in (400, 404)
        assert _delete(c, "/api/chats/legacy%2F..").status_code in (400, 404)
        assert (
            _delete(c, "/api/runs/run-x", origin="https://evil.example").status_code
            == 403
        )
        assert (
            TestClient(c.app)
            .request(
                "DELETE",
                "/api/runs/run-x",
                content="{}",
                headers={"content-type": "application/json"},
            )
            .status_code
            == 401
        )
    settings = _settings(tmp_path)
    for bad in ("", "..", "a/b", "../../etc"):
        with pytest.raises(DeleteError):
            delete_session(settings, bad)
    with pytest.raises(DeleteError):
        delete_run(settings, "session-abc")  # not a run id


def test_cli_delete(demo_repo, tmp_path):
    """CLI delete needs ``--yes``, removes runs and sessions, and flags unknown ids."""
    from scr_agents.pipeline import run_review

    repo, base, head = demo_repo
    settings = _settings(tmp_path)
    report, run_dir = run_review(settings, repo, base, head)
    _report2, run_dir2 = run_review(settings, repo, base, head)
    out = str(tmp_path / "out")
    cli = [sys.executable, "-m", "scr_agents.cli", "--out", out, "sessions", "delete"]
    refused = subprocess.run(
        [*cli, report.run_id],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert refused.returncode == 2 and "--yes" in refused.stderr and run_dir.exists()
    ok = subprocess.run(
        [*cli, report.run_id, run_dir2.parent.name, "--yes"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    assert (
        f"deleted {report.run_id} (run)" in ok.stdout
        and "session with 1 run(s)" in ok.stdout
    )
    assert not run_dir.exists() and not run_dir2.parent.exists()
    missing = subprocess.run(
        [*cli, "run-gone", "--yes"], capture_output=True, text=True, check=False
    )
    assert missing.returncode == 1 and "no such run" in missing.stderr
