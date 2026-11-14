"""MCP servers as folders switched on in mcp_servers/config.yaml: what a run gets.

Each folder holds one server's manifest, mcp_servers/<server>/mcp.yaml.
"""

import sys
from pathlib import Path

import pytest
import yaml

from scr_agents.config import MCPServerConfig, Settings
from scr_agents.llm.mcp_registry import MCPRegistry
from scr_agents.ws1_review.tools import mcp_offered

ECHO = Path(__file__).parent / "mcp_echo_server.py"


def _server(root: Path, folder: str, **manifest) -> Path:
    d = root / folder
    d.mkdir(parents=True)
    (d / "mcp.yaml").write_text(yaml.safe_dump(manifest))
    return d


def _settings(root: Path, **extra) -> Settings:
    return Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "mcp_servers_dir": str(root),
            "web_search": {"allowlist": ["cwe.mitre.org"]},
            **extra,
        }
    )


def test_folders_start_off_and_only_ready_servers_that_are_on_reach_a_run(
    tmp_path, monkeypatch
):
    """Folder servers start off; only switched-on, ready ones reach a run."""
    root = tmp_path / "mcp_servers"
    d = _server(
        root,
        "echo",
        name="echo",
        command=sys.executable,
        args=["{dir}/../srv.py", "--x"],
        env={"HOME_DIR": "{dir}"},
        env_from_host=["ECHO_KEY"],
        web_search={"tool_name": "search", "limit_arg": "limit"},
    )
    _server(
        root,
        "keyed",
        name="keyed",
        command=sys.executable,
        requires_env=["NEEDS_A_KEY"],
    )
    _server(
        root,
        "uninstalled",
        name="uninstalled",
        command=sys.executable,
        requires=["{dir}/dist/index.js"],
    )
    _server(root, "nocmd", name="nocmd", command="definitely-not-a-command-xyz")
    _server(root, "broken", name="bad name!", command="x")
    (root / "notes").mkdir()  # a folder without mcp.yaml is ignored
    monkeypatch.setenv("ECHO_KEY", "s3cret")
    monkeypatch.delenv("NEEDS_A_KEY", raising=False)
    reg = MCPRegistry(root)
    entries = {e.manifest.name: e for e in reg.entries()}
    assert set(entries) == {"echo", "keyed", "uninstalled", "nocmd"} and any(
        "bad name!" in x for x in reg.errors
    )
    assert not any(e.enabled for e in entries.values()), (
        "nothing in mcp_servers/ runs until someone switches it on"
    )
    assert entries["echo"].ready and entries["keyed"].missing == [
        "environment variable NEEDS_A_KEY"
    ]
    assert (
        entries["uninstalled"].missing[0].startswith("file ")
        and "not found" in entries["nocmd"].missing[0]
    )

    cfg = entries["echo"].server_config()
    assert cfg.args == [f"{d.resolve()}/../srv.py", "--x"] and cfg.env == {
        "HOME_DIR": str(d.resolve()),
        "ECHO_KEY": "s3cret",
    }
    summary = entries["echo"].summary()
    assert summary["env_keys"] == ["ECHO_KEY", "HOME_DIR"] and "s3cret" not in str(
        summary
    ), "no values to the console"

    settings = _settings(root)
    assert reg.apply(settings).mcp_servers == []
    for name in ("echo", "keyed", "uninstalled"):
        reg.set_server(name, enabled=True)
    live = reg.apply(settings)
    assert [s.name for s in live.mcp_servers] == ["echo"], (
        "switched on but not ready: not started"
    )
    assert live.web_search.enabled is False, "no web search until one is chosen"

    reg.set_server("echo", components=["validator", "bugs"])
    reg.set_web_search("echo")
    live = reg.apply(settings)
    assert live.mcp_servers[0].components == ["bugs", "validator"], (
        "kept in the canonical order"
    )
    ws = live.web_search
    assert (ws.enabled, ws.mcp_server, ws.tool_name, ws.limit_arg) == (
        True,
        "echo",
        "search",
        "limit",
    )
    assert ws.allowlist == ["cwe.mitre.org"], (
        "the allowlist stays a config-file decision"
    )
    state = yaml.safe_load((root / "config.yaml").read_text())
    assert state == {
        "servers": {
            "echo": {"enabled": True, "components": ["bugs", "validator"]},
            "keyed": {"enabled": True},
            "uninstalled": {"enabled": True},
        },
        "web_search": "echo",
    }

    reg.set_server("echo", enabled=False)
    assert reg.apply(settings).web_search.enabled is False, (
        "the web search server is off, so web search is off"
    )


def test_bad_edits_are_refused(tmp_path):
    """Unknown servers or components, and servers without web search, are refused."""
    root = tmp_path / "mcp_servers"
    _server(root, "plain", name="plain", command=sys.executable)
    reg = MCPRegistry(root)
    with pytest.raises(KeyError):
        reg.set_server("ghost", enabled=True)
    with pytest.raises(ValueError):
        reg.set_server("plain", components=["planner"])
    with pytest.raises(ValueError):
        reg.set_web_search("plain")  # no web_search section in its manifest
    with pytest.raises(KeyError):
        reg.set_web_search("ghost")
    assert not (root / "config.yaml").exists()


def test_config_file_servers_stay_on_and_win_a_name_clash(tmp_path):
    """A server from the config file replaces a folder server of the same name."""
    root = tmp_path / "mcp_servers"
    _server(root, "dup", name="dup", command=sys.executable, args=["from-folder"])
    reg = MCPRegistry(root)
    reg.set_server("dup", enabled=True)
    settings = _settings(
        root,
        mcp_servers=[
            {"name": "dup", "command": sys.executable, "args": ["from-config"]}
        ],
    )
    live = reg.apply(settings)
    assert [(s.name, s.args) for s in live.mcp_servers] == [("dup", ["from-config"])]


def test_components_limit_which_agents_get_the_tools():
    """``components`` limits which agents see a server; none listed means all."""
    s = Settings.load(
        overrides={
            "mcp_servers": [
                {"name": "a", "command": "x", "components": ["validator"]},
                {"name": "b", "command": "x"},
            ]
        }
    )
    assert mcp_offered(s, "a", "validator") and not mcp_offered(s, "a", "style")
    assert mcp_offered(s, "b", "style"), (
        "no components = every agent that takes MCP tools"
    )
    assert mcp_offered(s, "a", None), (
        "callers that do not say who they are get everything (old behaviour)"
    )
    assert MCPServerConfig(name="c", command="x").components == []


def test_the_review_tools_follow_the_components(tool_context, tmp_path):
    """A validator-only server's tools reach the validator but not the bug agent."""
    from scr_agents.llm.mcp_client import MCPClient
    from scr_agents.ws1_review.tools import review_tools, validator_tools

    if not MCPClient.available():
        pytest.skip("mcp extra not installed")
    servers = [
        MCPServerConfig(
            name="echo",
            command=sys.executable,
            args=[str(ECHO)],
            components=["validator"],
        )
    ]
    tool_context.settings = tool_context.settings.model_copy(
        update={"mcp_servers": servers}
    )
    client = MCPClient(servers).start()
    try:
        tool_context.mcp = client

        def names(tools):
            return {t.name for t in tools}

        assert "mcp_echo_add" not in names(review_tools(tool_context, component="bugs"))
        assert "mcp_echo_add" in names(
            review_tools(tool_context, component="validator")
        )
        assert "mcp_echo_add" in names(validator_tools(tool_context, scratch="F-1"))
    finally:
        client.close()


def test_shipped_mcp_servers_folder_is_valid():
    """The shipped ``mcp_servers/`` folders parse, each with a description and hint."""
    root = Path(__file__).resolve().parents[2] / "mcp_servers"
    reg = MCPRegistry(root)
    entries = {e.folder: e for e in reg.entries()}
    assert reg.errors == [] and {"web-search", "brave-search"} <= set(entries)
    assert (
        entries["web-search"].manifest.web_search.tool_name
        == "get-web-search-summaries"
    )
    assert entries["brave-search"].manifest.requires_env == ["BRAVE_API_KEY"]
    for e in entries.values():
        assert e.manifest.description and e.manifest.install, e.folder
