"""The console's Configure menu: MCP servers and skills switched on and off.

They are switched through the API. The API can change what is on and for whom, never
what a server runs.
"""

import sys
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from scr_agents.config import Settings
from scr_agents.llm.mcp_client import MCPClient
from scr_agents.web.server import create_app

TOKEN = "cfg-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ECHO = Path(__file__).parent / "mcp_echo_server.py"


@pytest.fixture
def setup(tmp_path):
    """A console client with an echo server, an uninstalled server and two skills."""
    servers_dir, skills = tmp_path / "mcp_servers", tmp_path / "skills"
    (servers_dir / "echo").mkdir(parents=True)
    (servers_dir / "echo" / "mcp.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "echo",
                "description": "test server",
                "command": sys.executable,
                "args": [str(ECHO)],
                "env": {"TOKEN_FOR_SERVER": "do-not-show"},
                "web_search": {"tool_name": "search", "limit_arg": "limit"},
            }
        )
    )
    (servers_dir / "later").mkdir()
    (servers_dir / "later" / "mcp.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "later",
                "command": sys.executable,
                "requires": ["{dir}/dist/index.js"],
                "install": "make",
            }
        )
    )
    for folder, name in (("shared_skills", "general"), ("bugs", "edges")):
        (skills / folder / name).mkdir(parents=True)
        (skills / folder / name / "SKILL.md").write_text(
            f"---\ndescription: {name} guidance\n---\nThe text of {name}.\n"
        )
    settings = Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "mcp_servers_dir": str(servers_dir),
            "skills": {"paths": [str(skills)]},
            "web_search": {"allowlist": ["cwe.mitre.org"]},
            "web": {"token": TOKEN, "allowed_hosts": ["testserver"]},
        }
    )
    with TestClient(create_app(settings, token=TOKEN), headers=AUTH) as c:
        yield c, servers_dir, skills


def test_mcp_servers_switch_on_choose_agents_and_web_search(setup):
    """The API switches servers and web search but never changes what a server runs."""
    c, servers_dir, _skills = setup
    d = c.get("/api/settings/mcp-servers").json()
    by = {s["name"]: s for s in d["servers"]}
    assert by["echo"]["enabled"] is False and by["echo"]["ready"] is True
    assert (
        by["later"]["ready"] is False
        and by["later"]["missing"][0].startswith("file ")
        and by["later"]["install"] == "make"
    )
    assert by["echo"]["env_keys"] == ["TOKEN_FOR_SERVER"] and "do-not-show" not in str(
        d
    ), "env values stay on the server"
    assert d["components"] == [
        "bugs",
        "vulnerabilities",
        "style",
        "validator",
        "orchestrator",
    ]

    d = c.post(
        "/api/settings/mcp-servers/echo",
        json={"enabled": True, "components": ["validator"]},
    ).json()
    assert {s["name"]: s for s in d["servers"]}["echo"]["components"] == ["validator"]
    d = c.post("/api/settings/web-search", json={"server": "echo"}).json()
    assert d["web_search"] == {
        "chosen": "echo",
        "enabled": True,
        "server": "echo",
        "tool": "search",
        "allowlist": ["cwe.mitre.org"],
    }
    cfg = c.get("/api/config").json()
    assert cfg["mcp_servers"] == ["echo"] and cfg["web_search"] is True, (
        "what a review started now would use"
    )

    # the API changes on/off and who gets the tools, never what runs
    before = (servers_dir / "echo" / "mcp.yaml").read_text()
    c.post(
        "/api/settings/mcp-servers/echo",
        json={"enabled": True, "command": "/bin/sh", "args": ["-c", "id"]},
    )
    assert (servers_dir / "echo" / "mcp.yaml").read_text() == before
    assert (
        "command"
        not in yaml.safe_load((servers_dir / "config.yaml").read_text())["servers"][
            "echo"
        ]
    )

    assert (
        c.post("/api/settings/mcp-servers/ghost", json={"enabled": True}).status_code
        == 404
    )
    assert (
        c.post(
            "/api/settings/mcp-servers/echo", json={"components": ["planner"]}
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/api/settings/mcp-servers/echo", json={"components": "validator"}
        ).status_code
        == 400
    )
    assert (
        c.post("/api/settings/web-search", json={"server": "later"}).status_code == 400
    )  # no web_search section
    assert c.post(
        "/api/settings/mcp-servers/..%2F..", json={"enabled": True}
    ).status_code in (
        400,
        404,
    )


@pytest.mark.skipif(not MCPClient.available(), reason="mcp extra not installed")
def test_the_test_button_starts_the_server_and_lists_its_tools(setup):
    """Test starts a ready server and lists its tools; an unready one says why."""
    c, _servers_dir, _skills = setup
    out = c.post("/api/settings/mcp-servers/echo/test", json={}).json()
    assert out["ok"] is True and {t["name"] for t in out["tools"]} == {"search", "add"}
    out = c.post("/api/settings/mcp-servers/later/test", json={}).json()
    assert (
        out["ok"] is False
        and out["error"].startswith("not ready")
        and out["install"] == "make"
    )
    assert c.post("/api/settings/mcp-servers/ghost/test", json={}).status_code == 404


def test_skills_listed_per_folder_viewed_and_switched_off(setup):
    """Skills are listed per agent folder, shown singly, and switched off on disk."""
    c, _servers_dir, skills = setup
    d = c.get("/api/settings/skills").json()
    folders = {f["folder"]: f for f in d["folders"]}
    assert (
        list(folders)[:2] == ["shared_skills", "planner"]
        and folders["planner"]["skills"] == []
    )
    assert [s["name"] for s in folders["bugs"]["skills"]] == ["edges"] and folders[
        "bugs"
    ]["label"] == "Bug reviewer"
    one = c.get("/api/settings/skills/edges").json()
    assert one["body"] == "The text of edges.\n" and one["component"] == "bugs"
    d = c.post("/api/settings/skills/edges", json={"enabled": False}).json()
    assert {f["folder"]: f for f in d["folders"]}["bugs"]["skills"][0][
        "enabled"
    ] is False
    assert yaml.safe_load((skills / "config.yaml").read_text()) == {
        "disabled": ["edges"]
    }
    assert (
        c.post("/api/settings/skills/edges", json={"enabled": "no"}).status_code == 400
    )
    assert c.get("/api/settings/skills/nope").status_code == 404


def test_settings_need_the_token_and_same_origin(setup):
    """Settings routes refuse callers without the token or from another origin."""
    c, _servers_dir, _skills = setup
    anon = TestClient(c.app)
    assert anon.get("/api/settings/mcp-servers").status_code == 401
    assert (
        anon.post("/api/settings/skills/edges", json={"enabled": False}).status_code
        == 401
    )
    assert (
        c.post(
            "/api/settings/mcp-servers/echo",
            json={"enabled": True},
            headers={"origin": "https://evil.example"},
        ).status_code
        == 403
    )
