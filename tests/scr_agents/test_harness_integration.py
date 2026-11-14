"""How scr_agents plugs into its surroundings.

MCP server manifests, GPUStack, .env, project memory.
"""

import os
import sys
from pathlib import Path

import pytest
import yaml

from scr_agents.cli import load_env
from scr_agents.config import Settings
from scr_agents.llm.mcp_registry import MCPRegistry
from scr_agents.ws5_knowledge.project_memory import ProjectMemory


def test_a_default_on_server_can_still_be_switched_off(tmp_path: Path) -> None:
    """mcp_servers/config.yaml wins over enabled_by_default and default_components."""
    d = tmp_path / "mcp_servers" / "kb"
    d.mkdir(parents=True)
    (d / "mcp.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "kb",
                "command": "{python}",
                "enabled_by_default": True,
                "default_components": ["validator"],
            }
        )
    )
    reg = MCPRegistry(tmp_path / "mcp_servers")
    (entry,) = reg.entries()
    assert entry.enabled and entry.components == ["validator"]
    assert entry.server_config().command == sys.executable
    reg.set_server("kb", enabled=False)
    (entry,) = reg.entries()
    assert not entry.enabled and entry.components == ["validator"]
    reg.set_server("kb", components=["bugs"])
    assert reg.entries()[0].components == ["bugs"]


def test_gpustack_settings_apply_unless_a_config_names_a_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GPUSTACK_BASE_URL / GPUSTACK_API_KEY are defaults, never overrides."""
    monkeypatch.setenv("GPUSTACK_BASE_URL", "https://gpustack.example/v1")
    monkeypatch.setenv("GPUSTACK_API_KEY", "key-1")
    s = Settings.load()
    assert (s.llm.local.base_url, s.llm.local.api_key) == (
        "https://gpustack.example/v1",
        "key-1",
    )
    s = Settings.load(
        overrides={
            "llm": {"local": {"base_url": "http://lan:8010/v1", "api_key": "lan"}}
        }
    )
    assert (s.llm.local.base_url, s.llm.local.api_key) == ("http://lan:8010/v1", "lan")
    assert "gemma4-31b" in s.llm.local.preferred_models, "the GPUStack model name"


def test_env_file_is_read_unless_turned_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.env`` in the working directory fills in unset variables."""
    (tmp_path / ".env").write_text(
        "GPUSTACK_API_KEY=from-dotenv\nSCR_AGENTS_MODE=offline\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SCR_AGENTS_MODE", raising=False)
    load_env()  # the test fixture sets SCR_AGENTS_NO_DOTENV
    assert "SCR_AGENTS_MODE" not in os.environ
    monkeypatch.delenv("SCR_AGENTS_NO_DOTENV")
    monkeypatch.setenv("SCR_AGENTS_MODE", "local")
    load_env()
    assert os.environ["GPUSTACK_API_KEY"] == "from-dotenv"
    assert os.environ["SCR_AGENTS_MODE"] == "local", "variables already set win"
    monkeypatch.delenv("GPUSTACK_API_KEY")


def test_a_repository_reviewed_before_the_rename_keeps_its_memory(
    tmp_path: Path,
) -> None:
    """An existing .codecheck/ folder is used until a .scr_agents/ one exists."""
    repo = tmp_path / "repo"
    (repo / ".codecheck").mkdir(parents=True)
    (repo / ".codecheck" / "memory.md").write_text("# notes\nUse tox to test.\n")
    mem = ProjectMemory(repo)
    assert mem.name == ".codecheck" and mem.exists and "tox" in mem.notes()
    mem.append_note("Second note.")
    assert "Second note." in (repo / ".codecheck" / "memory.md").read_text()
    assert ProjectMemory(tmp_path / "fresh").name == ".scr_agents"
    (repo / ".scr_agents").mkdir()
    assert ProjectMemory(repo).name == ".scr_agents", (
        "the new folder wins once it exists"
    )
