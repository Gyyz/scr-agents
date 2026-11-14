import json
import time

import pytest
from starlette.testclient import TestClient

from scr_agents.config import Settings
from scr_agents.web.server import create_app

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _settings(tmp_path, **web):
    return Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "projects_dir": str(tmp_path / "projects"),
            "scanners": {"enabled": ["builtin"]},
            "web": {
                "token": TOKEN,
                "allowed_hosts": ["testserver", "localhost"],
                **web,
            },
        }
    )


@pytest.fixture
def client(tmp_path):
    """A console client for an offline server, sending the test token."""
    with TestClient(create_app(_settings(tmp_path)), headers=AUTH) as c:
        yield c


def _wait_finished(client, run_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get(f"/api/runs/{run_id}").json()
        if s["status"] != "running":
            return s
        time.sleep(0.3)
    raise AssertionError("run did not finish")


def test_index_and_config(client):
    """The page loads and ``/api/config`` reports offline mode and a model."""
    assert "SCR Agents console" in client.get("/").text
    cfg = client.get("/api/config").json()
    assert cfg["mode"] == "offline" and cfg["reviewer_model"]


def test_demo_run_streams_events_and_report(client, tmp_path):
    """A demo run streams its events, serves its report and replays from disk."""
    run_id = client.post("/api/demo", json={}).json()["run_id"]
    # live stream: replays what already happened, then follows until the run ends
    events = []
    with client.stream("GET", f"/api/runs/{run_id}/events") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
                if events[-1]["event"] in ("run_finished", "run_failed"):
                    break
    kinds = [e["event"] for e in events]
    assert kinds[-1] == "run_finished", events[-1]
    assert (
        "stage" in kinds
        and "llm_call" in kinds
        and "tool_call" in kinds
        and "verdict" in kinds
    )
    assert "llm_start" in kinds and "tool_start" in kinds
    starts = [e for e in events if e["event"] == "tool_start"]
    assert starts and starts[0]["call_id"] and starts[0]["agent"]
    tool = next(e for e in events if e["event"] == "tool_call")
    assert "output_preview" in tool and "input" in tool
    stages = {e["stage"]: e["status"] for e in events if e["event"] == "stage"}
    assert stages == {
        s: "done"
        for s in (
            "inputs",
            "graph",
            "scanners",
            "bench",
            "review",
            "validation",
            "report",
        )
    }

    summary = _wait_finished(client, run_id)
    assert summary["status"] == "finished" and summary["counts"]["confirmed"] >= 1
    sid = client.get("/api/config").json()["session_id"]
    assert summary["session_id"] == sid and summary["run_dir"].endswith(
        f"/sessions/{sid}/{run_id}"
    )
    assert any(
        r["run_id"] == run_id and r["session_id"] == sid
        for r in client.get("/api/runs").json()
    )
    assert any(
        s["session_id"] == sid and run_id in s["runs"]
        for s in client.get("/api/sessions").json()
    )

    report = client.get(f"/api/runs/{run_id}/report").json()
    assert (
        report["report"]["findings"]
        and "# Code review evidence report" in report["markdown"]
    )
    fid = report["report"]["findings"][0]["finding"]["id"]
    rec = client.post(
        f"/api/runs/{run_id}/verdict",
        json={"finding": fid, "verdict": "accept", "note": "ok"},
    ).json()
    assert rec["verdict"] == "accept"

    # replay of a finished run from disk goes through the same endpoint
    with TestClient(create_app(_settings(tmp_path)), headers=AUTH) as c2:
        with c2.stream("GET", f"/api/runs/{run_id}/events") as r:
            replay = [
                json.loads(line[6:])
                for line in r.iter_lines()
                if line.startswith("data: ")
            ]
        assert (
            replay[-1]["event"] == "run_finished" and replay[-1].get("replay") is True
        )
        assert len(replay) == len(events)


def test_bad_review_request_returns_400(client):
    """A review of a missing repo is a 400, and an unknown run is a 404."""
    res = client.post("/api/review", json={"repo": "/nonexistent", "base": "main"})
    assert res.status_code == 400 and "error" in res.json()
    assert client.get("/api/runs/nope").status_code == 404


def test_api_requires_the_token(tmp_path):
    """API routes need the token, which ``/?token=`` turns into a strict cookie."""
    with TestClient(create_app(_settings(tmp_path))) as anon:
        assert anon.get("/").status_code == 200, (
            "the page itself is public; every /api route is not"
        )
        assert anon.get("/api/config").status_code == 401
        assert (
            anon.get("/api/runs", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        assert anon.post("/api/demo", json={}).status_code == 401
        # the browser flow: the printed URL carries the token once, then a SameSite
        # cookie does the work
        r = anon.get(f"/?token={TOKEN}", follow_redirects=False)
        assert r.status_code in (302, 303) and "scr_agents_token" in r.headers.get(
            "set-cookie", ""
        )
        assert (
            "samesite=strict" in r.headers["set-cookie"].lower()
            and "httponly" in r.headers["set-cookie"].lower()
        )
        assert anon.get("/api/config").status_code == 200, (
            "the cookie set by the redirect authenticates"
        )
        assert anon.get("/?token=wrong", follow_redirects=False).status_code == 401


def test_posts_need_json_and_a_matching_origin(client):
    """Posts must be JSON from the same origin; an unknown Host is refused."""
    r = client.post(
        "/api/demo", content="mode=anthropic", headers={"content-type": "text/plain"}
    )
    assert r.status_code == 415
    r = client.post("/api/demo", json={}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert (
        client.get("/api/config", headers={"host": "evil.example"}).status_code == 400
    ), "DNS rebinding"


def test_review_target_restrictions(client, demo_repo, tmp_path):
    """Local paths and file or SSH URLs are refused; a repo under projects/ works."""
    repo, base, _head = demo_repo
    for bad in (
        "/etc",
        str(repo),
        "file:///tmp/x",
        "ssh://git@host/x.git",
        "git@github.com:x/y.git",
    ):
        r = client.post("/api/review", json={"repo": bad, "base": base})
        assert r.status_code == 400 and "error" in r.json(), bad
    import shutil

    inside = tmp_path / "projects" / "demo-cached"
    shutil.copytree(repo, inside)
    r = client.post("/api/review", json={"repo": str(inside), "base": base})
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]
    assert _wait_finished(client, run_id)["status"] == "finished"


def test_local_paths_can_be_enabled_explicitly(tmp_path, demo_repo):
    """With ``allow_local_paths`` on, a local repository path can be reviewed."""
    repo, base, _head = demo_repo
    with TestClient(
        create_app(_settings(tmp_path, allow_local_paths=True)), headers=AUTH
    ) as c:
        r = c.post("/api/review", json={"repo": str(repo), "base": base})
        assert r.status_code == 201
        assert _wait_finished(c, r.json()["run_id"])["status"] == "finished"


def test_mode_cannot_be_chosen_by_the_client(client):
    """A ``mode`` in the request body is ignored; the server's mode is used."""
    run_id = client.post("/api/demo", json={"mode": "anthropic"}).json()["run_id"]
    _wait_finished(client, run_id)
    report = client.get(f"/api/runs/{run_id}/report").json()["report"]
    assert report["stats"]["mode"] == "offline"
