"""Every model API call and every tool call leaves a full, linked trace.

The trace holds what was sent and what came back.
"""

import json
import time

from starlette.testclient import TestClient

from scr_agents.config import Settings
from scr_agents.llm.backend import BackendError, BackendResponse, ToolSpec
from scr_agents.llm.local import LocalBackend
from scr_agents.llm.loop import PREVIEW_CHARS, ToolLoop
from scr_agents.web.server import create_app
from scr_agents.ws4_guardrails.safety_filter import SafetyFilter
from scr_agents.ws5_knowledge.activity_log import ActivityLog
from tests.scr_agents.test_local_backend import FakeServer

TOKEN = "trace-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Scripted:
    """Fake backend that asks for one ``big`` tool call, then answers in JSON."""

    name, model = "scripted", "scripted-model"

    def __init__(self):
        """Start with no calls made."""
        self.n = 0

    def complete(
        self, *, system, messages, tools, output_schema=None, max_tokens=16000
    ):
        """Return the tool call on the first turn and the final answer after it."""
        self.n += 1
        if self.n == 1:
            return BackendResponse(
                content=[
                    {"type": "tool_use", "id": "c1", "name": "big", "input": {"n": 3}}
                ],
                stop_reason="tool_use",
                usage={"input_tokens": 11, "output_tokens": 2},
                exchanges=[
                    {
                        "api": "fake",
                        "request": {"hello": "model"},
                        "response": {"ok": 1},
                    }
                ],
            )
        return BackendResponse(
            content=[{"type": "text", "text": '{"done": true}'}], stop_reason="end_turn"
        )


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_model_and_tool_calls_are_traced_in_full(tmp_path):
    """Each model and tool call writes a full trace file; events keep a preview."""
    log = ActivityLog(tmp_path / "run" / "activity.jsonl", "run-x", [])
    big = "x" * (PREVIEW_CHARS * 3)
    tools = [
        ToolSpec(
            "big",
            "returns a lot",
            {"type": "object", "properties": {"n": {"type": "integer"}}},
            lambda i: big,
        )
    ]
    res = ToolLoop(
        Scripted(), tools, SafetyFilter(), log, agent="subagent:bugs", max_turns=4
    ).run("the system prompt", "review this", output_schema={"type": "object"})
    log.close()
    assert res.ok and res.data == {"done": True}
    events = _events(tmp_path / "run" / "activity.jsonl")
    llm = [e for e in events if e["event"] == "llm_call"]
    tool = next(e for e in events if e["event"] == "tool_call")
    assert (
        len(llm) == 2
        and all(e["trace"].startswith("trace/") for e in llm)
        and tool["trace"]
    )
    assert len(tool["output_preview"]) == PREVIEW_CHARS  # the event keeps a preview...

    t1 = json.loads((tmp_path / "run" / llm[0]["trace"]).read_text())
    assert (
        t1["kind"] == "llm"
        and t1["agent"] == "subagent:bugs"
        and t1["turn"] == 1
        and t1["model"] == "scripted-model"
    )
    assert t1["request"]["system"] == "the system prompt"
    assert t1["request"]["messages"] == [
        {"role": "user", "content": "review this"}
    ]  # exactly what was sent then
    assert [t["name"] for t in t1["request"]["tools"]] == ["big"]
    assert (
        t1["response"]["content"][0]["name"] == "big"
        and t1["response"]["usage"]["input_tokens"] == 11
    )
    assert t1["exchanges"] == [
        {"api": "fake", "request": {"hello": "model"}, "response": {"ok": 1}}
    ]

    t2 = json.loads((tmp_path / "run" / llm[1]["trace"]).read_text())
    sent = t2["request"]["messages"]
    assert [m["role"] for m in sent] == [
        "user",
        "assistant",
        "user",
    ]  # the second call carried the tool result
    assert (
        sent[2]["content"][0]["type"] == "tool_result"
        and big in sent[2]["content"][0]["content"]
    )

    tt = json.loads((tmp_path / "run" / tool["trace"]).read_text())
    assert (
        tt["kind"] == "tool" and tt["input"] == {"n": 3} and big in tt["output"]
    )  # ...the trace the whole output
    names = sorted(p.name for p in (tmp_path / "run" / "trace").iterdir())
    assert len(names) == 3 and names[0].startswith("00001-")


def test_local_backend_records_exact_http_bodies_but_no_credentials(tmp_path):
    """The local backend keeps each exact HTTP body but never the API key."""
    reply = {"message": {"role": "assistant", "content": "plain prose, not json"}}
    repaired = {"message": {"role": "assistant", "content": '{"verdict": "confirmed"}'}}
    with FakeServer([reply, repaired]) as srv:
        b = LocalBackend(srv.base_url, "qwen3-local", api_key="sk-very-secret")
        r = b.complete(
            system="s",
            messages=[{"role": "user", "content": "u"}],
            tools=[],
            output_schema={"type": "object"},
        )
    assert len(r.exchanges) == 2 and r.exchanges[1]["purpose"] == "json repair"
    first = r.exchanges[0]
    assert (
        first["url"].endswith("/chat/completions")
        and first["status"] == 200
        and first["duration_s"] >= 0
    )
    assert first["request"] == srv.requests[0]  # byte-for-byte the body vLLM received
    assert (
        first["response"]["choices"][0]["message"]["content"] == "plain prose, not json"
    )
    assert "sk-very-secret" not in json.dumps(r.exchanges)


def test_failed_calls_are_traced_too(tmp_path):
    """Failed HTTP calls keep their exchanges and the loop traces the error."""
    with FakeServer([]) as srv:  # no scripted reply -> HTTP 500
        b = LocalBackend(srv.base_url, "m", retries=1)
        try:
            b.complete(
                system="s", messages=[{"role": "user", "content": "u"}], tools=[]
            )
            raise AssertionError("expected a BackendError")
        except BackendError as e:
            assert [x["status"] for x in e.exchanges] == [500, 500] and e.exchanges[0][
                "request"
            ]["model"] == "m"
        log = ActivityLog(tmp_path / "activity.jsonl", "r", [])
        res = ToolLoop(
            b, [], SafetyFilter(), log, agent="validator:F-1", max_turns=2
        ).run("s", "u")
        log.close()
    err = next(
        e for e in _events(tmp_path / "activity.jsonl") if e["event"] == "llm_error"
    )
    trace = json.loads((tmp_path / err["trace"]).read_text())
    assert not res.ok and "HTTP 500" in trace["error"] and len(trace["exchanges"]) == 2
    assert trace["request"]["messages"] == [{"role": "user", "content": "u"}]


def test_console_serves_traces_and_refuses_other_paths(tmp_path):
    """The console serves chat and run traces but nothing else in the run folder."""
    settings = Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "projects_dir": str(tmp_path / "out" / "projects"),
            "scanners": {"enabled": ["builtin"]},
            "web": {"token": TOKEN, "allowed_hosts": ["testserver"]},
        }
    )
    with TestClient(create_app(settings, token=TOKEN), headers=AUTH) as c:
        cid = c.post(
            "/api/chats/new", json={"demo": True, "text": "review the last commit"}
        ).json()["chat_id"]
        deadline = time.time() + 120
        while True:
            with c.stream("GET", f"/api/chats/{cid}/events?follow=0") as r:
                recs = [
                    json.loads(line[6:])
                    for line in r.iter_lines()
                    if line.startswith("data: ")
                ]
            if any(
                x.get("event") == "message"
                and x.get("role") == "assistant"
                and not x.get("intro")
                for x in recs
            ):
                break
            assert time.time() < deadline
            time.sleep(0.2)
        # the lead agent's own model calls are traced in the chat session
        lead = next(
            x for x in recs if x.get("event") == "llm_call" and x.get("agent") == "lead"
        )
        got = c.get(f"/api/chats/{cid}/trace/{lead['trace']}")
        assert got.status_code == 200 and got.json()["agent"] == "lead"
        assert "review the last commit" in json.dumps(got.json()["request"]["messages"])
        # the review's calls are traced in the run
        run_id = next(x for x in recs if x.get("event") == "run_started")["run"]
        run_events = [
            json.loads(line)
            for line in (settings_run_dir(c, run_id) / "activity.jsonl")
            .read_text()
            .splitlines()
        ]
        tool = next(e for e in run_events if e["event"] == "tool_call")
        name = tool["trace"].split("/", 1)[1]
        body = c.get(f"/api/runs/{run_id}/trace/{name}").json()
        assert body["kind"] == "tool" and body["tool"] == tool["tool"]
        # nothing outside the trace directory, and no traversal through the ids
        assert c.get(f"/api/runs/{run_id}/trace/..%2Factivity.jsonl").status_code in (
            400,
            404,
        )
        assert c.get(f"/api/runs/{run_id}/trace/report.json").status_code == 404
        assert c.get("/api/runs/..%2F..%2Fx/trace/a.json").status_code in (400, 404)
        assert c.get(f"/api/chats/{cid}/trace/nope.json").status_code == 404
        assert (
            TestClient(c.app).get(f"/api/runs/{run_id}/trace/{name}").status_code == 401
        )


def settings_run_dir(client, run_id):
    """Return the console's run folder for ``run_id``."""
    return client.app.state.manager.run_dir(run_id)


def test_mcp_tool_calls_carry_their_server_on_events_and_traces(tmp_path):
    """MCP tool calls carry ``origin`` and ``remote_name`` on events and traces."""

    class CallsMcp:
        name, model = "m", "m"

        def __init__(self):
            self.n = 0

        def complete(
            self, *, system, messages, tools, output_schema=None, max_tokens=16000
        ):
            self.n += 1
            if self.n == 1:
                return BackendResponse(
                    content=[
                        {
                            "type": "tool_use",
                            "id": "m1",
                            "name": "mcp_echo_add",
                            "input": {"a": 2, "b": 3},
                        }
                    ],
                    stop_reason="tool_use",
                )
            return BackendResponse(
                content=[{"type": "text", "text": "done"}], stop_reason="end_turn"
            )

    spec = ToolSpec(
        "mcp_echo_add",
        "[MCP echo] add",
        {"type": "object"},
        lambda i: str(i["a"] + i["b"]),
        untrusted=True,
        origin="mcp:echo",
        remote_name="add",
    )
    plain = ToolSpec("read_file", "read", {"type": "object"}, lambda i: "x")
    log = ActivityLog(tmp_path / "activity.jsonl", "r", [])
    ToolLoop(
        CallsMcp(), [spec, plain], SafetyFilter(), log, agent="lead", max_turns=3
    ).run("s", "u")
    log.close()
    events = _events(tmp_path / "activity.jsonl")
    start = next(e for e in events if e["event"] == "tool_start")
    call = next(e for e in events if e["event"] == "tool_call")
    assert (
        start["origin"] == call["origin"] == "mcp:echo" and call["remote_name"] == "add"
    )
    trace = json.loads((tmp_path / call["trace"]).read_text())
    assert (
        trace["origin"] == "mcp:echo"
        and trace["remote_name"] == "add"
        and "5" in trace["output"]
    )
    assert plain.origin == ""  # built-in tools carry no origin field at all
