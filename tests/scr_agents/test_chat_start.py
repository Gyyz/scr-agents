"""Starting a conversation on a repository that takes long to clone.

Seen live: a 2,537-file repo took over three minutes. Start must answer at once,
repeated presses must not race the clone, and a half-cloned directory must never be
used.
"""

import json
import threading
import time
from pathlib import Path

from starlette.testclient import TestClient

from scr_agents.config import Settings
from scr_agents.inputs import git as g
from scr_agents.inputs import project as project_mod
from scr_agents.inputs.project import project_name, resolve_project
from scr_agents.web.server import create_app

TOKEN = "start-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _settings(tmp_path):
    return Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "projects_dir": str(tmp_path / "out" / "projects"),
            "scanners": {"enabled": ["builtin"]},
            "web": {
                "token": TOKEN,
                "allowed_hosts": ["testserver"],
                "url_schemes": ["https", "file"],
            },
        }
    )


def _records(client, cid):
    with client.stream("GET", f"/api/chats/{cid}/events?follow=0") as r:
        return [
            json.loads(line[6:])
            for line in r.iter_lines()
            if line.startswith("data: ")
            and json.loads(line[6:]).get("event") != "replay_done"
        ]


def _wait(client, cid, predicate, timeout=120):
    deadline = time.time() + timeout
    while True:
        recs = _records(client, cid)
        if predicate(recs):
            return recs
        assert time.time() < deadline, [r.get("event") for r in recs][-12:]
        time.sleep(0.1)


def _slow_clone(monkeypatch, seconds):
    """Make every `git clone` take `seconds` longer, the way a large repository does."""
    real = g.git_cmd

    def slow(*args, **kw):
        if "clone" in args:
            time.sleep(seconds)
        return real(*args, **kw)

    monkeypatch.setattr(g, "git_cmd", slow)


def test_start_answers_at_once_and_clones_in_the_background(
    demo_repo, tmp_path, monkeypatch
):
    """Start replies at once; the slow clone runs behind it, then the review."""
    repo, _base, _head = demo_repo
    _slow_clone(monkeypatch, 1.5)
    with TestClient(create_app(_settings(tmp_path), token=TOKEN), headers=AUTH) as c:
        t0 = time.monotonic()
        res = c.post(
            "/api/chats/new",
            json={"repo": f"file://{repo}", "text": "review the last commit"},
        )
        assert res.status_code == 201, res.text
        assert time.monotonic() - t0 < 1.0, "Start must not wait for the clone"
        chat = res.json()
        assert chat["preparing"] is True and chat["title"] == "demo"
        cid = chat["chat_id"]
        assert any(
            x["chat_id"] == cid for x in c.get("/api/chats").json()
        )  # listed immediately

        recs = _wait(
            c,
            cid,
            lambda rs: any(
                r.get("event") == "message"
                and r.get("role") == "assistant"
                and not r.get("intro")
                for r in rs
            ),
        )
        kinds = [r["event"] for r in recs]
        assert (
            kinds.index("repo_preparing")
            < kinds.index("repo_ready")
            < kinds.index("run_started")
        )
        ready = next(r for r in recs if r["event"] == "repo_ready")
        assert ready["duration_s"] >= 1.5 and Path(ready["repo"]).is_dir()
        intro = next(r for r in recs if r.get("intro"))
        assert "What should I check?" not in intro["text"], (
            "the user already said what to check"
        )
        assert c.get(f"/api/chats/{cid}").json()["preparing"] is False


def test_repeated_presses_share_one_clone(demo_repo, tmp_path, monkeypatch):
    """Four presses of Start on one URL share a single clone and each get an intro."""
    repo, _base, head = demo_repo
    _slow_clone(monkeypatch, 1.0)
    with TestClient(create_app(_settings(tmp_path), token=TOKEN), headers=AUTH) as c:
        ids = [
            c.post("/api/chats/new", json={"repo": f"file://{repo}"}).json()["chat_id"]
            for _ in range(4)
        ]
        for cid in ids:
            recs = _wait(
                c,
                cid,
                lambda rs: any(
                    r.get("event") in ("repo_ready", "repo_failed") for r in rs
                ),
            )
            assert not any(r["event"] == "repo_failed" for r in recs), [
                r.get("error") for r in recs
            ]
            assert any(r.get("intro") for r in recs)
        clones = [p for p in (tmp_path / "out" / "projects").iterdir()]
        assert [p.name for p in clones] == [project_name(f"file://{repo}")], (
            "one clone, no partial leftovers"
        )
        assert g.resolve(clones[0], "HEAD") == head


def test_a_failed_clone_is_reported_in_the_chat_and_retried(tmp_path):
    """A clone error is posted as a reply; once the remote exists, a message retries."""
    with TestClient(create_app(_settings(tmp_path), token=TOKEN), headers=AUTH) as c:
        missing = tmp_path / "nope"
        cid = c.post(
            "/api/chats/new", json={"repo": f"file://{missing}", "text": "hi"}
        ).json()["chat_id"]
        recs = _wait(
            c,
            cid,
            lambda rs: (
                any(r.get("event") == "repo_failed" for r in rs)
                and not c.get(f"/api/chats/{cid}").json()["busy"]
            ),
        )
        err = next(
            r
            for r in recs
            if r.get("event") == "message" and r.get("role") == "assistant"
        )
        assert "could not get the repository" in err["text"].lower() and err["error"]
        assert not (tmp_path / "out" / "projects").exists() or not any(
            (tmp_path / "out" / "projects").iterdir()
        )
        # the remote appears later: the next message retries instead of failing forever
        g.git_cmd("init", "-q", "-b", "main", str(missing))
        (missing / "a.py").write_text("x = 1\n")
        g.git_cmd(
            "-C",
            str(missing),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "add",
            "a.py",
        )
        g.git_cmd(
            "-C",
            str(missing),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            "init",
        )
        assert (
            c.post(f"/api/chats/{cid}/messages", json={"text": "review it"}).status_code
            == 202
        )
        recs = _wait(c, cid, lambda rs: any(r.get("event") == "repo_ready" for r in rs))
        assert any(r.get("intro") for r in recs)


def test_concurrent_resolves_never_see_a_half_clone(demo_repo, tmp_path, monkeypatch):
    """Threads resolving one URL at once all get the finished clone, made once."""
    repo, _base, head = demo_repo
    _slow_clone(monkeypatch, 0.8)
    url, results, errors = f"file://{repo}", [], []

    def resolve():
        try:
            d = resolve_project(url, tmp_path / "projects")
            results.append(g.resolve(d, "HEAD"))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=resolve) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and results == [head] * 4
    assert [p.name for p in (tmp_path / "projects").iterdir()] == [project_name(url)]


def test_a_leftover_partial_clone_is_replaced(demo_repo, tmp_path):
    """A ``.git`` without commits, left by an interrupted clone, is cloned afresh."""
    repo, _base, head = demo_repo
    url = f"file://{repo}"
    dest = tmp_path / "projects" / project_name(url)
    dest.mkdir(parents=True)
    g.git_cmd(
        "init", "-q", str(dest)
    )  # a .git with no commits: what an interrupted clone leaves behind
    assert g.resolve(resolve_project(url, tmp_path / "projects"), "HEAD") == head
    assert project_mod  # module imported for monkeypatching in the other tests


def test_the_page_and_scripts_are_revalidated_on_every_load(tmp_path):
    """The page, app.js and style.css are sent with ``no-cache``; the API is not.

    Seen live: after an update the browser kept running the old app.js from its cache.
    """
    with TestClient(create_app(_settings(tmp_path), token=TOKEN)) as c:
        for path in ("/", "/static/app.js", "/static/style.css"):
            r = c.get(path)
            assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", (
                path
            )
        assert "no-cache" not in c.get("/api/config", headers=AUTH).headers.get(
            "cache-control", ""
        )
