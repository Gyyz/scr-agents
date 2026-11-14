"""MCP calls in the activity log.

Which servers a run had, and every tools/call the agents made, with the server's own
reply (before web-search filtering), so the console can show them next to the tool
calls.
"""

import json
import sys
from pathlib import Path

import pytest

from scr_agents.config import MCPServerConfig, Settings
from scr_agents.llm.backend import TOOL_CALLER
from scr_agents.llm.mcp_client import MCPClient
from scr_agents.ws4_guardrails.safety_filter import SafetyFilter

pytestmark = pytest.mark.skipif(
    not MCPClient.available(), reason="mcp extra not installed"
)
SERVER = Path(__file__).parent / "mcp_echo_server.py"


class _Log:
    def __init__(self, tmp_path):
        self.records, self.traces, self.dir = [], {}, tmp_path

    def log(self, event, agent=None, **payload):
        self.records.append({"event": event, "agent": agent, **payload})

    def write_trace(self, name, data):
        self.traces[name] = data
        return f"trace/{name}.json"


def test_each_call_is_logged_with_its_caller_and_secrets_redacted(tmp_path):
    """Each MCP call is logged and traced with its calling agent, secrets redacted."""
    log = _Log(tmp_path)
    client = MCPClient(
        [
            MCPServerConfig(name="echo", command=sys.executable, args=[str(SERVER)]),
            MCPServerConfig(name="ghost", command="/nonexistent/server"),
        ],
        connect_timeout=20,
        log=log,
        safety=SafetyFilter(),
    ).start()
    try:
        status = {s["name"]: s for s in client.status()}
        assert (
            status["echo"]["status"] == "connected"
            and status["echo"]["command"] == Path(sys.executable).name
        )
        assert {t["name"] for t in status["echo"]["tools"]} == {"search", "add"}
        assert (
            status["ghost"]["status"] == "failed"
            and status["ghost"]["error"]
            and not status["ghost"]["tools"]
        )
        assert "args" not in status["echo"] and "env" not in status["echo"], (
            "never report commands' arguments"
        )

        key = (
            "AKIA" + "ABCDEFGHIJKLMNOP"
        )  # shaped like an AWS key: must not reach the log or the trace
        token = TOOL_CALLER.set(
            {"agent": "subagent:vulnerabilities", "call_id": "toolu_1"}
        )
        try:
            client.call_tool("echo", "search", {"query": key, "limit": 2})
        finally:
            TOOL_CALLER.reset(token)
        start, done = [
            r for r in log.records if r["event"] in ("mcp_start", "mcp_call")
        ]
        assert start["event"] == "mcp_start" and done["event"] == "mcp_call"
        for r in (start, done):
            assert (
                r["agent"] == "subagent:vulnerabilities" and r["call_id"] == "toolu_1"
            )
            assert r["server"] == "echo" and r["tool"] == "search"
        assert (
            done["is_error"] is False
            and done["error"] is None
            and done["duration_s"] >= 0
        )
        assert "evil.example.net" in done["output_preview"], (
            "the raw reply, before the allowlist filter"
        )
        assert (
            key not in done["output_preview"] and "REDACTED" in done["output_preview"]
        )
        trace = log.traces["mcp-echo-search"]
        assert done["trace"] == "trace/mcp-echo-search.json" and trace["kind"] == "mcp"
        assert trace["request"] == {
            "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": key, "limit": 2}},
        }
        assert (
            key not in json.dumps(trace["response"])
            and trace["response"]["isError"] is False
        )

        with pytest.raises(
            RuntimeError
        ):  # a call outside any agent, with bad arguments
            client.call_tool("echo", "add", {"a": "x"})
        failed = log.records[-1]
        assert (
            failed["event"] == "mcp_call"
            and failed["is_error"] is True
            and failed["agent"] == "mcp:echo"
        )
    finally:
        client.close()


def test_a_review_logs_the_servers_and_links_mcp_calls_to_tool_calls(
    demo_repo, tmp_path
):
    """A review logs its MCP servers and ties each ``mcp_call`` to its tool call."""
    from scr_agents.pipeline import run_review

    repo, base, head = demo_repo
    echo = {"command": sys.executable, "args": [str(SERVER)]}
    settings = Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "scanners": {"enabled": ["builtin"]},
            "mcp_servers": [{"name": "websearch", **echo}, {"name": "tools", **echo}],
            "web_search": {
                "enabled": True,
                "mcp_server": "websearch",
                "tool_name": "search",
                "limit_arg": "limit",
                "allowlist": ["cwe.mitre.org"],
            },
        }
    )
    _report, run_dir = run_review(settings, repo, base, head)
    recs = [
        json.loads(line)
        for line in (run_dir / "activity.jsonl").read_text().splitlines()
    ]

    servers = next(r for r in recs if r["event"] == "mcp_servers")
    assert servers["web_search"] == "websearch"
    exposed = {
        s["name"]: {t["name"]: t["agent_tool"] for t in s["tools"]}
        for s in servers["servers"]
    }
    assert exposed == {
        "websearch": {"search": "web_search", "add": None},
        "tools": {"search": "mcp_tools_search", "add": "mcp_tools_add"},
    }

    tool = next(
        r for r in recs if r["event"] == "tool_call" and r["tool"] == "web_search"
    )
    assert tool["origin"] == "mcp:websearch" and tool["remote_name"] == "search"
    mcp = next(r for r in recs if r["event"] == "mcp_call")
    assert (mcp["agent"], mcp["call_id"], mcp["server"]) == (
        tool["agent"],
        tool["call_id"],
        "websearch",
    )
    assert (
        "evil.example.net" in mcp["output_preview"]
        and "evil.example.net" not in tool["output_preview"]
    )
    order = [r["seq"] for r in recs if r.get("call_id") == tool["call_id"]]
    kinds = [r["event"] for r in recs if r.get("call_id") == tool["call_id"]]
    assert kinds == [
        "tool_start",
        "mcp_start",
        "mcp_call",
        "tool_call",
    ] and order == sorted(order)
    assert (run_dir / mcp["trace"]).is_file()
