"""Tools exposed to the WS1 agents.

Review tools are read-only; validator tools add the WS3 test bench and sandboxed shell.
Every tool whose output derives from untrusted content is marked `untrusted` so the loop
routes it through the safety filter.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scr_agents.config import Settings
from scr_agents.inputs import git as g
from scr_agents.llm.backend import ToolSpec
from scr_agents.models import (
    BenchReport,
    GraphReport,
    ReviewRequest,
    ScannerReport,
    split_lines,
)
from scr_agents.ws2_analysis.code_graph import CodeGraph
from scr_agents.ws3_harness.shell import SandboxedShell
from scr_agents.ws3_harness.test_bench import TestBench
from scr_agents.ws3_harness.workspace import Workspace
from scr_agents.ws4_guardrails.safety_filter import SafetyFilter
from scr_agents.ws5_knowledge.knowledge_base import KnowledgeBase
from scr_agents.ws5_knowledge.skills import SkillLibrary

MAX_FILE_LINES = 400
SCRATCH_DIR = (
    ".scr_agents_scratch"  # the only place the validator may write, inside a checkout
)
MAX_WRITE_CHARS = 200_000


@dataclass
class ToolContext:
    """What the agent tools of one review run work on.

    The request and its checkouts, the analysis results (code graph, scanners, test
    bench), the knowledge base, the guardrails and the optional web search, MCP client,
    project memory and skills. The review agent and the validator share one per run.
    """

    settings: Settings
    request: ReviewRequest
    workspace: Workspace
    graph: CodeGraph
    graph_report: GraphReport
    scanner_report: ScannerReport
    kb: KnowledgeBase
    safety: SafetyFilter
    shell: SandboxedShell
    bench: TestBench
    bench_report: BenchReport | None = None
    web_search: Any | None = None  # WebSearch
    mcp: Any | None = None  # MCPClient
    memory: Any | None = None  # ProjectMemory (.scr_agents/ in the reviewed repo)
    skills: SkillLibrary | None = (
        None  # operator-authored guidance (skills/<name>/SKILL.md)
    )
    prior_round: Any | None = None  # PriorRound: the round before this one, if any
    # one per run: tools with parallel_safe=False (run_tests, run_command) take it, so
    # parallel validators never build or run in the shared worktrees at the same time
    serial_lock: threading.Lock = field(default_factory=threading.Lock)


def _safe_path(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if not str(p).startswith(str(root.resolve()) + "/") and p != root.resolve():
        raise ValueError(f"path '{rel}' escapes the workspace")
    return p


def _obj(
    name: str,
    description: str,
    props: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


def mcp_offered(settings: Settings, server: str, component: str | None) -> bool:
    """May `component` use this MCP server's tools?

    `component` is bugs, vulnerabilities, style, validator or orchestrator. A server
    without `components` is for all of them; no component given means no filter.
    """
    cfg = next((s for s in settings.mcp_servers if s.name == server), None)
    return (
        cfg is None
        or not cfg.components
        or component is None
        or component in cfg.components
    )


def review_tools(ctx: ToolContext, component: str | None = None) -> list[ToolSpec]:
    """The read-only tools.

    `component` is the agent they are for (a subagent's name or "validator"): it picks
    its skills (skills/shared_skills + skills/<component>) and the MCP servers assigned
    to it.
    """
    req, ws = ctx.request, ctx.workspace

    def list_changed_files(_: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "path": f.path,
                "status": f.status,
                "language": f.language,
                "added": f.added,
                "removed": f.removed,
                "changed_lines": f.changed_lines()[:200],
            }
            for f in req.files
        ]

    def get_diff(inp: dict[str, Any]) -> str:
        f = req.file(inp["path"])
        if f is None:
            raise FileNotFoundError(f"{inp['path']} is not part of this change")
        return f.diff_text()

    def read_file(inp: dict[str, Any]) -> str:
        root = ws.dir_for(inp.get("ref") or "head")
        p = _safe_path(root, inp["path"])
        if not p.is_file():
            raise FileNotFoundError(inp["path"])
        lines = split_lines(p.read_text(encoding="utf-8", errors="replace"))
        start = max(1, int(inp.get("start_line") or 1))
        end = min(len(lines), int(inp.get("end_line") or start + MAX_FILE_LINES - 1))
        body = "\n".join(f"{i:5d}| {lines[i - 1]}" for i in range(start, end + 1))
        return (
            f"{inp['path']} lines {start}-{end} of {len(lines)} "
            f"({inp.get('ref') or 'head'})\n{body}"
        )

    def search_code(inp: dict[str, Any]) -> str:
        root = ws.head_dir
        pattern, glob, limit = (
            inp["pattern"],
            inp.get("glob"),
            int(inp.get("max_results") or 50),
        )
        if shutil.which("rg"):
            cmd = [
                "rg",
                "-n",
                "--no-heading",
                "--color",
                "never",
                "-m",
                str(limit),
                "-e",
                pattern,
            ]
            if glob:
                cmd += ["-g", glob]
            proc = subprocess.run(
                cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            return proc.stdout[:20000] or "(no matches)"
        rx = re.compile(pattern)
        out: list[str] = []
        for path, text in ctx.graph.files.items():
            if glob and not Path(path).match(glob):
                continue
            for i, line in enumerate(split_lines(text), 1):
                if rx.search(line):
                    out.append(f"{path}:{i}:{line}")
                    if len(out) >= limit:
                        return "\n".join(out)
        return "\n".join(out) or "(no matches)"

    def code_graph_query(inp: dict[str, Any]) -> dict[str, Any]:
        return ctx.graph.query(inp["symbol"])

    def scanner_findings(inp: dict[str, Any]) -> list[dict[str, Any]]:
        path = inp.get("path")
        return [
            f.model_dump()
            for f in ctx.scanner_report.findings
            if not path or f.path == path
        ]

    def kb_lookup(inp: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            e.to_dict() for e in ctx.kb.lookup(inp["query"], int(inp.get("limit") or 5))
        ]

    def memory_read(_: dict[str, Any]) -> dict[str, Any]:
        if ctx.memory is None:
            return {"exists": False, "notes": "", "note": "no project memory loaded"}
        summary: dict[str, Any] = ctx.memory.summary()
        return summary

    def git_log(inp: dict[str, Any]) -> list[dict[str, Any]]:
        n = int(inp.get("n") or 10)
        if inp.get("path"):
            commits = g.log_for_path(req.repo_path, inp["path"], n=n, ref=req.head)
        else:
            commits = req.commits
        return [c.model_dump() for c in commits[:n]]

    def skill_list(_: dict[str, Any]) -> list[dict[str, Any]]:
        if ctx.skills is None:
            return []
        return [
            s.summary()
            for s in ctx.skills.applicable(change_languages(req), component=component)
        ]

    def skill_read(inp: dict[str, Any]) -> dict[str, Any]:
        skill = (
            ctx.skills.get(str(inp.get("name") or ""), component=component)
            if ctx.skills is not None
            else None
        )
        if skill is None:
            return {
                "error": f"unknown skill {inp.get('name')!r}; "
                "skill_list shows what is available"
            }
        return {
            "name": skill.name,
            "description": skill.description,
            "body": skill.body,
        }

    tools = [
        ToolSpec(
            "list_changed_files",
            "List files changed between base and head with their changed line numbers.",
            _obj("", "", {}),
            list_changed_files,
            untrusted=True,
        ),
        ToolSpec(
            "get_diff",
            "Unified diff of one changed file.",
            _obj("", "", {"path": {"type": "string"}}, ["path"]),
            get_diff,
            untrusted=True,
        ),
        ToolSpec(
            "read_file",
            "Read a file (numbered lines) from the head or base checkout.",
            _obj(
                "",
                "",
                {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "ref": {"type": "string", "enum": ["head", "base"]},
                },
                ["path"],
            ),
            read_file,
            untrusted=True,
        ),
        ToolSpec(
            "search_code",
            "Regex search across the head checkout (ripgrep syntax).",
            _obj(
                "",
                "",
                {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                ["pattern"],
            ),
            search_code,
            untrusted=True,
        ),
        ToolSpec(
            "code_graph_query",
            "Definitions, callers and callees of a symbol (change blast radius).",
            _obj("", "", {"symbol": {"type": "string"}}, ["symbol"]),
            code_graph_query,
            untrusted=True,
        ),
        ToolSpec(
            "scanner_findings",
            "Static scanner findings on the changed lines, optionally for one path.",
            _obj("", "", {"path": {"type": "string"}}),
            scanner_findings,
            untrusted=True,
        ),
        ToolSpec(
            "kb_lookup",
            "Look up CWE / CERT C / MISRA knowledge by keywords or id.",
            _obj(
                "",
                "",
                {"query": {"type": "string"}, "limit": {"type": "integer"}},
                ["query"],
            ),
            kb_lookup,
        ),
        ToolSpec(
            "git_log",
            "Commits in the reviewed range, or recent history of one path.",
            _obj("", "", {"path": {"type": "string"}, "n": {"type": "integer"}}),
            git_log,
            untrusted=True,
        ),
        ToolSpec(
            "memory_read",
            "Project memory from .scr_agents/ in the repository: team notes, known "
            "false positives, previous runs, findings humans rejected.",
            _obj("", "", {}),
            memory_read,
            untrusted=True,
        ),
        # skills are operator content (like the system prompts), never from the reviewed
        # repository: trusted
        ToolSpec(
            "skill_list",
            "Review guidance (skills) written by the operators that applies to the "
            "languages of this change: names and descriptions.",
            _obj("", "", {}),
            skill_list,
        ),
        ToolSpec(
            "skill_read",
            "Full text of one skill by name. Read the ones that match the code you are "
            "reviewing.",
            _obj("", "", {"name": {"type": "string"}}, ["name"]),
            skill_read,
        ),
    ]
    if (
        ctx.web_search is not None
        and ctx.web_search.available()
        and mcp_offered(
            ctx.settings, ctx.settings.web_search.mcp_server or "", component
        )
    ):
        tools.append(ctx.web_search.toolspec())
    if ctx.mcp is not None:
        for server in ctx.mcp.servers:
            if (
                ctx.mcp.has_server(server)
                and server != ctx.settings.web_search.mcp_server
                and mcp_offered(ctx.settings, server, component)
            ):
                tools.extend(ctx.mcp.toolspecs(server))
    return tools


def agent_tool_name(settings: Settings, server: str, tool: str) -> str | None:
    """The name the agents know an MCP server's tool by (see review_tools).

    That is `mcp_<server>_<tool>`, or `web_search` for the search tool of the web-search
    server, whose other tools are not offered (``None``).
    """
    ws = settings.web_search
    if server == ws.mcp_server:
        return "web_search" if ws.enabled and tool == ws.tool_name else None
    return f"mcp_{server}_{tool}"


_MISSING_PY = re.compile(r"ModuleNotFoundError: No module named '([\w.]+)'")
_MISSING_NODE = re.compile(r"Cannot find module '([^']+)'")
_NO_SCRIPT = re.compile(r"can't open file|No such file or directory")


POC_TIMEOUT_S = 60
TIMEOUT_HINT = (
    "the process did not exit within {s}s; a script that starts a server must send its "
    "request, print the result, close the server and exit (or call the route handler "
    "or the function it uses directly, without listening on a port). Pass a larger "
    "`timeout` only for a slow build or test."
)


def run_with(path: str) -> list[str] | None:
    """How to run a scratch file, so the model need not guess the interpreter.

    Returns:
        The argv for a Python, JavaScript or TypeScript file, otherwise ``None``.
    """
    if path.endswith(".py"):
        return ["python3", path]
    if path.endswith((".js", ".mjs", ".cjs")):
        return ["node", path]
    if path.endswith(".ts"):
        return ["node", "-r", "ts-node/register", path]
    return None


def run_hint(argv: list[str], output: str, root: Path) -> str | None:
    """Turn the common proof-of-concept setup failures into an instruction.

    The failures are a missing Python module or Node package, a relative ``require``
    path and a script that does not exist; the hint tells the model what to do instead.

    Returns:
        The hint, or ``None`` when ``output`` shows none of these failures.
    """
    m = _MISSING_PY.search(output)
    if m:
        top = m.group(1).split(".")[0]
        local = (root / top).exists() or (root / f"{top}.py").exists()
        if not local:
            return (
                f"'{top}' is a third-party package that is not installed, and the "
                "sandbox is offline so it cannot be installed. Rewrite the script with "
                "the standard library (urllib.request, json, subprocess, socket) or "
                "only modules present in the checkout."
            )
        if (root / top).is_dir() and any((root / top).rglob("*.ts")):
            return (
                f"'{top}' holds TypeScript, which Python cannot import. Write the "
                "proof of concept in JavaScript and run it with node (or node -r "
                "ts-node/register for .ts files)."
            )
        return (
            f"'{top}' exists in the checkout; PYTHONPATH already points at the "
            "checkout root, check the import path."
        )
    m = _MISSING_NODE.search(output)
    if m:
        name = m.group(1)
        if name.startswith((".", "/")):
            return (
                "relative require paths resolve from the script's own directory "
                "(.scr_agents_scratch/...); require project files by absolute path, "
                "e.g. require(process.cwd() + '/src/x'), or by a bare path ('src/x') "
                "since NODE_PATH includes the checkout root."
            )
        if not (root / "node_modules").is_dir():
            return (
                f"'{name}' is not installed (no node_modules) and the sandbox cannot "
                "download packages; use Node's built-in modules (http, fetch, "
                "child_process, fs, assert)."
            )
        return (
            f"'{name}' is not in node_modules; use Node's built-in modules or a "
            "dependency the project already has."
        )
    if _NO_SCRIPT.search(output) and any(
        a.startswith(".scr_agents_scratch") for a in argv
    ):
        return (
            "the script was not found; call write_file first and run exactly the "
            "'path' it returns."
        )
    return None


def change_languages(req: ReviewRequest) -> set[str]:
    """Return the languages of the changed files, which decide the skills that apply."""
    return {f.language for f in req.files if f.language}


def validator_tools(
    ctx: ToolContext, scratch: str | None = None, component: str | None = "validator"
) -> list[ToolSpec]:
    """Review tools plus the harness: build/test, run a command, and write files.

    Proof-of-concept files go into `<checkout>/.scr_agents_scratch/[<scratch>/]`, never
    anywhere else.
    """
    ws = ctx.workspace
    scratch_rel = f"{SCRATCH_DIR}/{scratch}" if scratch else SCRATCH_DIR

    def run_tests(inp: dict[str, Any]) -> dict[str, Any]:
        return ctx.bench.run(
            inp.get("ref") or "head", inp.get("what") or "test"
        ).model_dump()

    def write_file(inp: dict[str, Any]) -> dict[str, Any]:
        # Models pass either "poc.py" or the full "<scratch_rel>/poc.py" they will later
        # run: accept both.
        rel = str(inp["path"]).strip()
        rel = rel.removeprefix("./")  # never strip "..": _safe_path must see it
        for prefix in (scratch_rel + "/", SCRATCH_DIR + "/"):
            rel = rel.removeprefix(prefix)
        content = str(inp.get("content") or "")
        if len(content) > MAX_WRITE_CHARS:
            raise ValueError(f"content exceeds {MAX_WRITE_CHARS} characters")
        written = []
        # The scratch directory is scr_agents's, not the repository's: write the file
        # into both checkouts so the same proof of concept can be run on base and head
        # and compared.
        for ref in ("base", "head"):
            scratch_dir = (ws.dir_for(ref) / scratch_rel).resolve()
            target = _safe_path(scratch_dir, rel)
            if target == scratch_dir:
                raise ValueError("path must name a file inside the scratch directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(ref)
        path = f"{scratch_rel}/{rel}"
        return {
            "path": path,
            "bytes": len(content.encode("utf-8")),
            "refs": written,
            "run_with": run_with(path),
        }

    def resolve_scratch(arg: str, root: Path) -> str:
        """Map a scratch path lacking the per-finding directory onto the written file.

        Models often drop the per-finding directory (.scr_agents_scratch/poc.py for
        .scr_agents_scratch/F-1/poc.py). Map such an argument onto the file write_file
        created.
        """
        if (
            scratch_rel == SCRATCH_DIR
            or not arg.startswith(SCRATCH_DIR + "/")
            or arg.startswith(scratch_rel + "/")
        ):
            return arg
        if (root / arg).exists():
            return arg
        candidate = f"{scratch_rel}/{arg[len(SCRATCH_DIR) + 1 :]}"
        return candidate if (root / candidate).exists() else arg

    def run_command(inp: dict[str, Any]) -> dict[str, Any]:
        root = ws.dir_for(inp.get("ref") or "head")
        cwd = _safe_path(root, inp["cwd"]) if inp.get("cwd") else root
        argv = [resolve_scratch(a, root) for a in inp["argv"]]
        # the checkout root is importable from proof-of-concept scripts (python3
        # path/poc.py otherwise is not)
        env = {
            "PYTHONPATH": str(root),
            "NODE_PATH": f"{root}{os.pathsep}{root / 'node_modules'}",
        }
        # a proof of concept should finish in seconds; a long default only hides scripts
        # that never exit
        timeout = int(inp.get("timeout") or POC_TIMEOUT_S)
        res = ctx.shell.run(argv, cwd=cwd, timeout=timeout, env_extra=env)
        out = res.to_dict()
        hint = (
            TIMEOUT_HINT.format(s=timeout)
            if res.timed_out
            else run_hint(argv, res.stdout + "\n" + res.stderr, root)
        )
        if hint:
            out["hint"] = hint
        return out

    tools = review_tools(ctx, component)
    tools += [
        ToolSpec(
            "run_tests",
            "Build or test the base or head checkout with the detected/configured "
            "command.",
            _obj(
                "",
                "",
                {
                    "ref": {"type": "string", "enum": ["head", "base"]},
                    "what": {"type": "string", "enum": ["test", "build"]},
                },
            ),
            run_tests,
            untrusted=True,
            parallel_safe=False,
        ),
        ToolSpec(
            "write_file",
            f"Write a proof-of-concept script or input file into the scratch directory "
            f"{scratch_rel}/ (the only writable place). It is written into both the "
            "base and the head checkout, so run_command can run it on either, e.g. "
            f"['python3', '{scratch_rel}/poc.py'] with ref head and again with ref "
            "base. Give `path` relative to the scratch directory (e.g. 'poc.py'). The "
            "checkout root is on PYTHONPATH.",
            _obj(
                "",
                "",
                {"path": {"type": "string"}, "content": {"type": "string"}},
                ["path", "content"],
            ),
            write_file,
            parallel_safe=False,
        ),
        ToolSpec(
            "run_command",
            "Run an allowlisted binary inside the base or head checkout to reproduce a "
            f"bug (default timeout {POC_TIMEOUT_S}s), "
            "e.g. ['python3', '.scr_agents_scratch/poc.py'] or "
            "['node', '.scr_agents_scratch/poc.js']. argv is not a shell: $(...), "
            "pipes, redirection and globs are passed literally, so put such logic in a "
            "script. The sandbox is offline and cannot install packages: use the "
            "standard library and what the checkout already contains (e.g. "
            "urllib.request, not requests).",
            _obj(
                "",
                "",
                {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "ref": {"type": "string", "enum": ["head", "base"]},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                ["argv"],
            ),
            run_command,
            untrusted=True,
            parallel_safe=False,
        ),
    ]
    return tools


def dumps(obj: Any) -> str:
    """Serialise ``obj`` as indented JSON, writing ``str(value)`` for other types."""
    return json.dumps(obj, default=str, indent=1)
