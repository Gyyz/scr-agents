"""Settings: packaged defaults merged with a user YAML and env overrides.

The packaged defaults are ``configs/default.yaml``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = PACKAGE_DIR / "configs" / "default.yaml"
PROMPTS_DIR = PACKAGE_DIR / "prompts"
KNOWLEDGE_DIR = PACKAGE_DIR / "knowledge"
SKILLS_DIR = PACKAGE_DIR / "skills"  # the skills shipped with the package
DEFAULT_BASE_URL = (
    "http://127.0.0.1:8000/v1"  # llm.local.base_url in configs/default.yaml
)

Effort = Literal["low", "medium", "high", "xhigh", "max"]
Orchestrator = Literal["deterministic", "openflow"]
ORCHESTRATORS: tuple[str, ...] = ("deterministic", "openflow")


class LocalLLMConfig(BaseModel):
    """OpenAI-compatible server (vLLM, SGLang, llama.cpp, Ollama...).

    Nothing leaves the host.
    """

    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: str = "EMPTY"
    # empty = first served model matching preferred_models, else the first listed
    model: str = ""
    preferred_models: list[str] = Field(
        default_factory=lambda: [
            "gemma-4-31b",
            "gemma4-31b",
            "gemma-4",
            "gemma4",
            "llama-3.3-70b",
            "llama-3.1-70b",
            "llama-3-70b",
            "llama-3",
        ]
    )
    validator_model: str = (
        ""  # empty = same as model; set to a second served model for independence
    )
    validator_base_url: str = ""  # empty = same server
    temperature: float = 0.2
    max_tokens: int = 8000
    timeout_s: float = 600.0
    json_repair: bool = (
        True  # re-ask with response_format when the final answer is not JSON
    )
    extra_body: dict[str, Any] = Field(
        default_factory=dict
    )  # e.g. {chat_template_kwargs: {enable_thinking: false}}
    # tokens; used when the server does not report max_model_len for the model. The
    # tool loop compacts old results near it
    context_window: int = 65536
    cache_metrics_interval_s: float = (
        5.0  # sample the servers' /metrics prefix-cache counters during a run; 0 = off
    )


class OpenAIConfig(BaseModel):
    """A hosted OpenAI-compatible API, OpenAI's by default (``llm.mode: openai``).

    The code under review is sent to that provider. The API key is read from the
    environment variable named in ``api_key_env``, never from a config file.
    """

    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    model: str = ""  # required: the provider's model id for the reviewers
    validator_model: str = ""  # empty = same as model
    # None = not sent: several hosted models accept only their default temperature
    temperature: float | None = None
    max_tokens: int = 8000
    # the request field that caps the output; older models and other providers
    # may want "max_tokens"
    token_param: Literal["max_completion_tokens", "max_tokens"] = (
        "max_completion_tokens"
    )
    timeout_s: float = 600.0
    json_repair: bool = True
    extra_body: dict[str, Any] = Field(default_factory=dict)
    # tokens; the API does not report it. The tool loop compacts old results near it
    context_window: int = 128000


class LLMConfig(BaseModel):
    """Backend choice, per-role models, and agent turn and parallelism limits."""

    mode: Literal["auto", "local", "openai", "anthropic", "offline"] = (
        "auto"  # auto = local if reachable, else offline; never an online API
    )
    local: LocalLLMConfig = Field(default_factory=LocalLLMConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    reviewer_model: str = (
        "claude-opus-5-5"  # only used with mode: anthropic (explicit opt-in)
    )
    validator_model: str = "claude-sonnet-5-5"  # only used with mode: anthropic
    planner_model: str | None = None  # defaults to reviewer_model
    effort: Effort = "high"
    validator_effort: Effort = "high"
    max_tokens: int = 16000
    max_turns: int = 24
    server_fallbacks: bool = True  # server-side refusal fallbacks (Claude API only)
    thinking_display: Literal["omitted", "summarized"] = "omitted"
    parallel_subagents: int = 3
    parallel_validations: int = 3
    max_findings_to_validate: int = 20
    max_style_findings_to_validate: int = (
        5  # style/naming candidates get the read-only validator, few of them
    )
    style_validation_turns: int = 6


class ScannerConfig(BaseModel):
    """Static scanners to run and how their results are filtered."""

    enabled: list[str] = Field(
        default_factory=lambda: [
            "builtin",
            "ruff",
            "bandit",
            "semgrep",
            "cppcheck",
            "gosec",
        ]
    )
    only_changed_lines: bool = True
    timeout_s: int = 300
    semgrep_config: str = "auto"


DENIED_ARGS: dict[str, list[str]] = {
    "python": ["-c"],
    "python3": ["-c"],
    "node": ["-e", "--eval", "-p", "--print", "-i", "--interactive"],
    "find": [
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-delete",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-fls",
    ],
    "git": [
        "-c",
        "-C",
        "--exec-path",
        "--git-dir",
        "--work-tree",
        "--config-env",
        "--namespace",
    ],
    "rg": ["--pre", "--pre-glob"],
}
ALLOWED_SUBCOMMANDS: dict[str, list[str]] = {
    "git": [
        "log",
        "show",
        "diff",
        "status",
        "blame",
        "grep",
        "ls-files",
        "ls-tree",
        "rev-parse",
        "cat-file",
        "describe",
        "rev-list",
        "shortlog",
        "name-rev",
    ],
    "npm": ["test", "run", "run-script", "ci", "install", "ls"],
}
PYTHON_MODULES = ["pytest", "unittest", "py_compile", "compileall", "doctest"]
ALLOWED_PATH_PREFIXES = [
    "/usr",
    "/opt",
    "/lib",
    "/lib64",
    "/bin",
    "/sbin",
    "/dev/null",
    "/nix",
    "/Library/Developer",
]


class ShellConfig(BaseModel):
    """Sandboxed shell policy: allowed binaries, argument rules and environment."""

    allowed_binaries: list[str] = Field(default_factory=list)
    timeout_s: int = 120
    max_output_chars: int = 20000
    # HOME is deliberately absent: the sandbox gets a throwaway one under its root
    env_passthrough: list[str] = Field(
        default_factory=lambda: [
            "PATH",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "VIRTUAL_ENV",
            "RUSTUP_HOME",
            "GOPATH",
            "GOCACHE",
            "GOMODCACHE",
        ]
    )
    # per-binary flags that turn a tool into a shell or let it leave the worktree
    denied_args: dict[str, list[str]] = Field(
        default_factory=lambda: {k: list(v) for k, v in DENIED_ARGS.items()}
    )
    # per-binary subcommand allowlist (binaries not listed accept any subcommand)
    allowed_subcommands: dict[str, list[str]] = Field(
        default_factory=lambda: {k: list(v) for k, v in ALLOWED_SUBCOMMANDS.items()}
    )
    python_modules: list[str] = Field(
        default_factory=lambda: list(PYTHON_MODULES)
    )  # python -m <module>
    # absolute paths in arguments must be under the sandbox root or one of these
    allowed_path_prefixes: list[str] = Field(
        default_factory=lambda: list(ALLOWED_PATH_PREFIXES)
    )


class BenchConfig(BaseModel):
    """How the test bench builds and tests the base and head checkouts."""

    runner: Literal["local", "docker", "qemu"] = "local"
    build_cmd: list[str] | None = None
    test_cmd: list[str] | None = None
    timeout_s: int = 600
    docker_image: str | None = None
    qemu: dict[str, Any] = Field(default_factory=dict)


# What an MCP server's replies are in; "auto" recognises each reply by its MIME type
# or its content (see llm/mcp_results.py)
ResultFormat = Literal["auto", "json", "csv", "tsv", "markdown", "html", "xml", "text"]


class MCPServerConfig(BaseModel):
    """An MCP server to launch over stdio, and which agents get its tools."""

    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    untrusted: bool = True  # results pass through the safety filter
    components: list[str] = Field(
        default_factory=list
    )  # agents that get the tools (bugs, vulnerabilities,
    # style, validator); empty = all of them
    result_format: ResultFormat = "auto"  # what its replies are in


class WebSearchConfig(BaseModel):
    """Allowlisted web search through one tool of one MCP server.

    See ``deploy/mcp/README.md``.
    """

    enabled: bool = False
    mcp_server: str | None = None  # name of an entry in mcp_servers
    # the search tool; defaults match deploy/mcp/web-search-mcp
    tool_name: str = "get-web-search-summaries"
    query_arg: str = "query"  # parameter carrying the query
    limit_arg: str = "limit"  # parameter carrying the result count
    allowlist: list[str] = Field(
        default_factory=list
    )  # result hosts kept (suffix match); everything else dropped
    max_results: int = 5


class SafetyConfig(BaseModel):
    """Limits and switches for the ``SafetyFilter`` that wraps untrusted text."""

    max_chars: int = 60000
    redact_secrets: bool = True
    block_on_injection: bool = False


class RegistryConfig(BaseModel):
    """Where the signed prompt manifest is, and whether prompts must be approved."""

    manifest: str | None = None  # defaults to the packaged prompts/manifest.yaml
    require_approved: bool = True


class KnowledgeConfig(BaseModel):
    """Extra CWE/CERT/MISRA knowledge base entries beyond the packaged ones."""

    paths: list[str] = Field(
        default_factory=list
    )  # extra directories of *.yaml entries


class SkillsConfig(BaseModel):
    """Skill roots: shared skills plus one folder per agent (see ``skills/README.md``).

    Shared skills live in ``<root>/shared_skills/<name>/SKILL.md``. ``builtin`` is
    the package's own ``scr_agents/skills``; other entries are directories, relative
    ones resolved against the working directory.
    """

    paths: list[str] = Field(default_factory=lambda: ["builtin"])


class ReviewConfig(BaseModel):
    """Review rounds: the validator's verdicts can send findings back for review.

    After a round, the findings the person reviewing (or, with ``--auto-rounds``, the
    validator) did not settle can be reviewed again, with the earlier review and
    validation reports as context, until ``max_rounds`` rounds have run in total.
    """

    max_rounds: int = Field(default=3, ge=1)
    # who runs a round: ``deterministic`` (planner, the three subagents in parallel,
    # merge and rank in code, then the validator) or ``openflow`` (an LLM gets an
    # overview of the change and tools, and decides which reviewers to run on what,
    # which candidates to validate and which to report; see ``ws1_review.openflow``)
    orchestrator: Orchestrator = "deterministic"
    openflow_max_turns: int = Field(default=30, ge=2)  # the orchestrator's tool turns


class WebConfig(BaseModel):
    """Web console settings: the ``/api`` routes need a token, the page does not.

    The console can start builds and tests of any repository it is pointed at, so
    every ``/api`` route needs the token; the page itself is public.
    """

    # bearer token; empty = generated when `serve` starts and printed once.
    # Env: SCR_AGENTS_TOKEN
    token: str = ""
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "[::1]"]
    )  # Host header
    allow_local_paths: bool = (
        False  # may the API review local paths outside projects_dir?
    )
    url_schemes: list[str] = Field(
        default_factory=lambda: ["https"]
    )  # clone URL schemes accepted from the API


class Settings(BaseModel):
    """All settings: packaged defaults, a user YAML, overrides and env vars."""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    scanners: ScannerConfig = Field(default_factory=ScannerConfig)
    shell: ShellConfig = Field(default_factory=ShellConfig)
    bench: BenchConfig = Field(default_factory=BenchConfig)
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    # folder of MCP servers, one <dir>/<server>/mcp.yaml each; <dir>/config.yaml says
    # which are on
    mcp_servers_dir: str = "mcp_servers"  # relative to the working directory
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    output_dir: str = "out"
    projects_dir: str = "out/projects"  # where git URLs are cloned for review
    max_diff_chars: int = 40000
    whole_repo_max_files: int = 400  # cap for scope=all reviews

    @classmethod
    def load(
        cls, path: str | Path | None = None, overrides: dict[str, Any] | None = None
    ) -> Settings:
        """Merge the packaged defaults, a user YAML and overrides, then apply env vars.

        ``GPUSTACK_BASE_URL`` and ``GPUSTACK_API_KEY`` replace the local server URL
        and key only while they are still the defaults. ``SCR_AGENTS_MODE``,
        ``SCR_AGENTS_TOKEN``, ``SCR_AGENTS_ORCHESTRATOR`` and
        ``SCR_AGENTS_MCP_SERVERS_DIR`` set ``llm.mode``, ``web.token``,
        ``review.orchestrator`` and ``mcp_servers_dir``; the last one is skipped when
        ``overrides`` sets ``mcp_servers_dir``, and the orchestrator one when
        ``overrides`` sets ``review.orchestrator``.

        Args:
            path: User YAML merged over the defaults; ``None`` for defaults only.
            overrides: Nested dict merged over the user YAML, e.g. from CLI flags.

        Returns:
            The validated settings.
        """
        data: dict[str, Any] = yaml.safe_load(DEFAULT_CONFIG.read_text()) or {}
        if path:
            user = yaml.safe_load(Path(path).read_text()) or {}
            data = deep_merge(data, user)
        if overrides:
            data = deep_merge(data, overrides)
        # GPUStack, the company-hosted OpenAI-compatible server (see .env.example),
        # unless the config files name a server themselves
        local = data.setdefault("llm", {}).setdefault("local", {})
        if (
            os.environ.get("GPUSTACK_BASE_URL")
            and local.get("base_url") == DEFAULT_BASE_URL
        ):
            local["base_url"] = os.environ["GPUSTACK_BASE_URL"]
        if (
            os.environ.get("GPUSTACK_API_KEY")
            and local.get("api_key", "EMPTY") == "EMPTY"
        ):
            local["api_key"] = os.environ["GPUSTACK_API_KEY"]
        env_mode = os.environ.get("SCR_AGENTS_MODE")
        if env_mode:
            data.setdefault("llm", {})["mode"] = env_mode
        env_token = os.environ.get("SCR_AGENTS_TOKEN")
        if env_token:
            data.setdefault("web", {})["token"] = env_token
        env_orch = os.environ.get("SCR_AGENTS_ORCHESTRATOR")
        if env_orch and "orchestrator" not in (overrides or {}).get("review", {}):
            data.setdefault("review", {})["orchestrator"] = env_orch
        env_dir = os.environ.get(
            "SCR_AGENTS_MCP_SERVERS_DIR"
        )  # e.g. a deployment's own folder; the tests use an empty one
        if env_dir and not (overrides or {}).get("mcp_servers_dir"):
            data["mcp_servers_dir"] = env_dir
        return cls.model_validate(data)

    def skill_paths(self) -> list[Path]:
        """The skill roots to load.

        ``builtin`` is resolved to the package's skills folder.
        """
        return [SKILLS_DIR if p == "builtin" else Path(p) for p in self.skills.paths]

    def manifest_path(self) -> Path:
        """Return ``registry.manifest`` if set, else the packaged prompt manifest."""
        return (
            Path(self.registry.manifest)
            if self.registry.manifest
            else PROMPTS_DIR / "manifest.yaml"
        )


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return ``base`` with ``override`` merged in, recursing into nested dicts.

    Values from ``override`` win; neither input is modified.
    """
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out
