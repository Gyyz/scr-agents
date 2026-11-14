import sys
from pathlib import Path

import pytest

from scr_agents.config import MCPServerConfig, WebSearchConfig
from scr_agents.llm.mcp_client import MCPClient
from scr_agents.ws4_guardrails.safety_filter import SafetyFilter
from scr_agents.ws4_guardrails.web_search import WebSearch

pytestmark = pytest.mark.skipif(
    not MCPClient.available(), reason="mcp extra not installed"
)
SERVER = Path(__file__).parent / "mcp_echo_server.py"


@pytest.fixture
def mcp():
    """A started client for the echo server that must close without errors."""
    client = MCPClient(
        [MCPServerConfig(name="echo", command=sys.executable, args=[str(SERVER)])]
    ).start()
    assert not client.errors, client.errors
    yield client
    client.close()
    assert client.errors == {}, client.errors


def test_mcp_tools_round_trip(mcp):
    """The echo server's tools are offered as untrusted agent tools that work."""
    specs = {t.name: t for t in mcp.toolspecs("echo")}
    assert {"mcp_echo_search", "mcp_echo_add"} <= set(specs)
    assert specs["mcp_echo_add"].handler({"a": 2, "b": 3}) == "5"
    assert specs["mcp_echo_add"].untrusted is True


def test_web_search_allowlist_and_filter(mcp):
    """Web search through MCP drops results from hosts not on the allowlist."""
    cfg = WebSearchConfig(
        enabled=True,
        mcp_server="echo",
        tool_name="search",
        limit_arg="limit",
        allowlist=["cwe.mitre.org"],
    )
    ws = WebSearch(cfg, mcp, SafetyFilter())
    assert ws.available()
    out = ws.toolspec().handler({"query": "command injection"})
    assert "evil.example.net" not in out
    assert "cwe.mitre.org" in out and '"dropped_not_allowlisted": 1' in out


def test_close_is_clean_after_a_normal_session():
    """Closing a client after normal use records no errors and drops the server."""
    client = MCPClient(
        [MCPServerConfig(name="echo", command=sys.executable, args=[str(SERVER)])]
    ).start()
    assert client.has_server("echo") and client.toolspecs("echo")
    client.close()
    assert client.errors == {}, client.errors
    assert not client.has_server("echo")


def test_mcp_check_command_lists_tools_and_probes_web_search(tmp_path, capsys):
    """``mcp check`` lists tools and probes web search; a dead server exits 1."""
    from scr_agents import cli

    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "llm:\n  mode: offline\n"
        f"output_dir: {tmp_path / 'out'}\n"
        "mcp_servers:\n"
        f"  - name: echo\n    command: {sys.executable}\n    args: ['{SERVER}']\n"
        "web_search:\n  enabled: true\n  mcp_server: echo\n"
        "  tool_name: search\n  query_arg: query\n"
        "  limit_arg: limit\n  allowlist: [cwe.mitre.org]\n"
    )
    assert (
        cli.main(["mcp", "check", "--config", str(cfg), "--query", "command injection"])
        == 0
    )
    out = capsys.readouterr().out
    assert "echo" in out and "mcp_echo_search" in out and "mcp_echo_add" in out
    assert "cwe.mitre.org" in out and "evil.example.net" not in out, (
        "the probe goes through the allowlist wrapper"
    )

    broken = tmp_path / "broken.yaml"
    broken.write_text(
        f"llm:\n  mode: offline\noutput_dir: {tmp_path / 'out'}\n"
        "mcp_servers:\n  - name: ghost\n    command: /nonexistent/server\n"
    )
    assert cli.main(["mcp", "check", "--config", str(broken)]) == 1
    assert "ghost" in capsys.readouterr().out
