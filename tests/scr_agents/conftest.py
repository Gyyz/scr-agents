from pathlib import Path

import pytest

from scr_agents.config import Settings
from scr_agents.demo import make_demo_repo
from scr_agents.inputs.request import build_request
from scr_agents.ws1_review.tools import ToolContext
from scr_agents.ws2_analysis.code_graph import CodeGraph
from scr_agents.ws2_analysis.scanners import run_scanners
from scr_agents.ws3_harness import (
    test_bench,
)
from scr_agents.ws3_harness.shell import SandboxedShell
from scr_agents.ws3_harness.workspace import Workspace
from scr_agents.ws4_guardrails.registry import PromptRegistry
from scr_agents.ws4_guardrails.safety_filter import SafetyFilter
from scr_agents.ws5_knowledge.knowledge_base import KnowledgeBase


@pytest.fixture(autouse=True)
def _isolated_from_local_settings(tmp_path_factory, monkeypatch):
    """Keep the developer's local MCP console state and ``.env`` out of every test.

    The checkout's ``mcp_servers/config.yaml`` is the developer's console state: a
    server switched on there must not start in the tests. Tests that use the registry
    pass ``mcp_servers_dir`` explicitly.
    """
    monkeypatch.setenv(
        "SCR_AGENTS_MCP_SERVERS_DIR", str(tmp_path_factory.mktemp("no-mcp-servers"))
    )
    # a developer's .env (GPUStack keys) must not point the tests at a real model server
    monkeypatch.setenv("SCR_AGENTS_NO_DOTENV", "1")
    monkeypatch.delenv("GPUSTACK_BASE_URL", raising=False)
    monkeypatch.delenv("GPUSTACK_API_KEY", raising=False)


@pytest.fixture
def demo_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A fresh planted-defect demo git repository as ``(path, base_sha, head_sha)``."""
    repo = tmp_path / "demo"
    base, head = make_demo_repo(repo)
    return repo, base, head


@pytest.fixture
def tool_context(demo_repo, tmp_path):
    """An offline ``ToolContext`` over the demo repo's change, builtin scanner only."""
    repo, base, head = demo_repo
    settings = Settings.load(
        overrides={
            "llm": {"mode": "offline", "parallel_validations": 2},
            "output_dir": str(tmp_path / "out"),
            "scanners": {"enabled": ["builtin"]},
        }
    )
    request = build_request(repo, base, head)
    with Workspace(request, tmp_path / "run") as ws:
        graph = CodeGraph.build(ws.head_dir)
        yield ToolContext(
            settings=settings,
            request=request,
            workspace=ws,
            graph=graph,
            graph_report=graph.blast_radius(request.files),
            scanner_report=run_scanners(settings.scanners, ws.head_dir, request.files),
            kb=KnowledgeBase([]),
            safety=SafetyFilter(settings.safety),
            shell=SandboxedShell(settings.shell, ws.root),
            bench=test_bench.TestBench(settings.bench, settings.shell, ws),
        )


@pytest.fixture
def registry(tmp_path):
    """The default signed prompt registry, approval required, stats in ``tmp_path``."""
    return PromptRegistry(
        Settings.load().manifest_path(), True, stats_path=tmp_path / "stats.jsonl"
    )
