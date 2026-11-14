"""MCP servers kept as folders, one manifest each, plus a file saying which are on.

``mcp_servers/<server>/mcp.yaml`` says how a server starts; ``mcp_servers/config.yaml``
says which ones are on, which agents get their tools and which one backs
``web_search``.

The manifests are operator files on disk. The console edits only `config.yaml`
(enabled, components, web search), never a command, its arguments or its environment,
so the web API cannot make the host run anything that is not already written in a
manifest. Servers listed under `mcp_servers` in the YAML config keep working as before
and are always on.

`apply(settings)` returns the settings a run uses: `mcp_servers` = the enabled, ready
manifests plus the configured ones; `web_search` bound to the chosen server (the
allowlist stays in the YAML config).
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from scr_agents.config import MCPServerConfig, ResultFormat, Settings

MANIFEST = "mcp.yaml"
STATE = "config.yaml"
# agents that take MCP tools (the planner has no tools; the chat lead agent has its own
# set; the orchestrator runs only with review.orchestrator: openflow)
COMPONENTS = ["bugs", "vulnerabilities", "style", "validator", "orchestrator"]
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
STATE_HEADER = (
    "# Which MCP servers (mcp_servers/<folder>/mcp.yaml) are on. Written by the\n"
    "# console (Configure -> MCP servers); editing by hand is fine. components: the\n"
    f"# agents that get the tools (empty = all of {', '.join(COMPONENTS)}).\n"
    "# web_search: the server behind the agents' web_search tool.\n"
)


class WebSearchSpec(BaseModel):
    """How to call a server's search tool when it backs ``web_search``.

    Names the tool and the arguments that take the query and the result limit.
    """

    tool_name: str
    query_arg: str = "query"
    limit_arg: str = "limit"


class MCPManifest(BaseModel):
    """The contents of ``mcp_servers/<server>/mcp.yaml``: how one MCP server starts.

    Also lists what the server needs before it can start and how to install it, both
    shown in the console.
    """

    name: str
    description: str = ""
    homepage: str = ""
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    env_from_host: list[str] = Field(
        default_factory=list
    )  # copied from scr_agents's own environment when set
    requires: list[str] = Field(
        default_factory=list
    )  # paths that must exist (placeholders: see expand)
    requires_env: list[str] = Field(
        default_factory=list
    )  # environment variables that must be set
    untrusted: bool = True
    # what its replies are in: auto (recognised per reply), json, csv, tsv, markdown,
    # html, xml or text. Set it when a server's replies are mistaken for another format
    result_format: ResultFormat = "auto"
    # how to install it, shown when something is missing; never run by scr_agents
    install: str = ""
    web_search: WebSearchSpec | None = None  # this server can back the web_search tool
    # Until mcp_servers/config.yaml says otherwise. Only for first-party servers that
    # ship in this repository: third-party ones stay off until someone switches them on
    enabled_by_default: bool = False
    default_components: list[str] = Field(default_factory=list)


class ServerEntry(BaseModel):
    """One server folder: its manifest, its state from ``config.yaml`` and readiness."""

    folder: str
    dir: str
    manifest: MCPManifest
    enabled: bool = False
    components: list[str] = Field(default_factory=list)
    missing: list[str] = Field(
        default_factory=list
    )  # why it cannot start (empty = ready)

    @property
    def ready(self) -> bool:
        """Whether the server can start: nothing it needs is missing."""
        return not self.missing

    def expand(self, text: str) -> str:
        """Fill in the manifest placeholders.

        ``{dir}`` is this server's folder, ``{root}`` the project root (the folder
        holding ``mcp_servers/``) and ``{python}`` the interpreter running scr_agents.
        """
        root = str(Path(self.dir).parent.parent)
        return (
            text.replace("{dir}", self.dir)
            .replace("{root}", root)
            .replace("{python}", sys.executable)
        )

    def server_config(self) -> MCPServerConfig:
        """Build the config that starts this server, with the placeholders filled in.

        Variables named in ``env_from_host`` are copied from this process's environment
        when they are set there.
        """
        m = self.manifest
        env = {k: self.expand(v) for k, v in m.env.items()}
        env.update({k: os.environ[k] for k in m.env_from_host if k in os.environ})
        return MCPServerConfig(
            name=m.name,
            command=self.expand(m.command),
            args=[self.expand(a) for a in m.args],
            env=env,
            untrusted=m.untrusted,
            components=list(self.components),
            result_format=m.result_format,
        )

    def summary(self) -> dict[str, Any]:
        """For the console: what it is and its state.

        The environment's values are left out (keys only).
        """
        cfg = self.server_config()
        m = self.manifest
        return {
            "name": m.name,
            "folder": self.folder,
            "description": m.description,
            "homepage": m.homepage,
            "command": [cfg.command, *cfg.args],
            "env_keys": sorted({*m.env, *m.env_from_host}),
            "untrusted": m.untrusted,
            "result_format": m.result_format,
            "enabled": self.enabled,
            "components": self.components,
            "ready": self.ready,
            "missing": self.missing,
            "install": m.install,
            "web_search": m.web_search.model_dump() if m.web_search else None,
            "source": "folder",
        }


class MCPRegistry:
    """The server folders under one ``mcp_servers/`` directory and their on/off state.

    Problems met while reading the manifests or ``config.yaml`` are collected in
    ``errors`` instead of being raised. Edits are serialised by a lock.
    """

    def __init__(self, root: str | Path):
        """Use ``root`` as the folder holding the server folders and ``config.yaml``."""
        self.root = Path(root)
        self.errors: list[str] = []
        self._lock = threading.Lock()

    # ------------------------------------------- state (mcp_servers/config.yaml)
    def _state(self) -> dict[str, Any]:
        try:
            data = yaml.safe_load((self.root / STATE).read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            return {"servers": {}, "web_search": None}
        except (OSError, yaml.YAMLError) as e:
            self.errors.append(f"{self.root / STATE}: {str(e).splitlines()[0]}")
            return {"servers": {}, "web_search": None}
        if not isinstance(data, dict):
            self.errors.append(f"{self.root / STATE}: not a mapping; ignored")
            return {"servers": {}, "web_search": None}
        raw = data.get("servers")
        servers = raw if isinstance(raw, dict) else {}
        return {
            "servers": {str(k): v for k, v in servers.items() if isinstance(v, dict)},
            "web_search": data.get("web_search")
            if isinstance(data.get("web_search"), str)
            else None,
        }

    def _save(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / (STATE + ".tmp")
        tmp.write_text(
            STATE_HEADER + yaml.safe_dump(state, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(self.root / STATE)

    # ----------------------------------------------------------------- manifests
    def entries(self) -> list[ServerEntry]:
        """Read each server folder's manifest and merge in its ``config.yaml`` state.

        Unreadable, invalid and duplicate manifests are skipped and reported in
        ``errors``, which each call resets.
        """
        self.errors = []
        state = self._state()
        out: list[ServerEntry] = []
        names: set[str] = set()
        if not self.root.is_dir():
            return out
        for d in sorted(
            p for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".")
        ):
            f = d / MANIFEST
            if not f.is_file():
                continue
            try:
                raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                manifest = MCPManifest.model_validate(raw)
            except (OSError, yaml.YAMLError, ValidationError) as e:
                self.errors.append(f"{f}: {str(e).splitlines()[0]}")
                continue
            if not NAME.match(manifest.name):
                self.errors.append(
                    f"{f}: name {manifest.name!r} must be letters, digits, - or _"
                )
                continue
            if manifest.name in names:
                self.errors.append(
                    f"{f}: duplicate server name {manifest.name!r} ignored"
                )
                continue
            names.add(manifest.name)
            st = state["servers"].get(manifest.name, {})
            enabled = (
                bool(st["enabled"]) if "enabled" in st else manifest.enabled_by_default
            )
            chosen = st.get("components", manifest.default_components)
            comps = [c for c in (chosen or []) if c in COMPONENTS]
            entry = ServerEntry(
                folder=d.name,
                dir=str(d.resolve()),
                manifest=manifest,
                enabled=enabled,
                components=comps,
            )
            entry.missing = self._missing(entry)
            out.append(entry)
        return out

    @staticmethod
    def _missing(entry: ServerEntry) -> list[str]:
        m = entry.manifest
        missing = [
            f"file {entry.expand(p)}"
            for p in m.requires
            if not Path(entry.expand(p)).exists()
        ]
        missing += [
            f"environment variable {k}" for k in m.requires_env if not os.environ.get(k)
        ]
        cmd = entry.expand(m.command)
        if not (Path(cmd).is_file() if "/" in cmd else shutil.which(cmd)):
            missing.append(f"command {cmd!r} not found")
        return missing

    def web_search_server(self) -> str | None:
        """Return the name of the server chosen to back ``web_search``, or None."""
        chosen = self._state()["web_search"]
        return chosen if isinstance(chosen, str) else None

    # ----------------------------------------------------------------- console edits
    def set_server(
        self,
        name: str,
        enabled: bool | None = None,
        components: list[str] | None = None,
    ) -> None:
        """Switch a server on or off, or choose its agents, and save ``config.yaml``.

        Args:
            name: The server name from its manifest.
            enabled: New on/off state, or None to leave it.
            components: Agents that get the server's tools (empty for all), or None to
                leave them.

        Raises:
            KeyError: No server has that name.
            ValueError: ``components`` names an unknown agent.
        """
        with self._lock:
            if name not in {e.manifest.name for e in self.entries()}:
                raise KeyError(f"no MCP server named {name!r} in {self.root}")
            bad = [c for c in components or [] if c not in COMPONENTS]
            if bad:
                raise ValueError(
                    f"unknown component(s) {bad}; choose from {COMPONENTS}"
                )
            state = self._state()
            cur = dict(state["servers"].get(name, {}))
            if enabled is not None:
                cur["enabled"] = bool(enabled)
            if components is not None:
                cur["components"] = [c for c in COMPONENTS if c in components]
            state["servers"][name] = cur
            self._save(state)

    def set_web_search(self, name: str | None) -> None:
        """Choose the server that backs ``web_search`` (None for none) and save it.

        Raises:
            KeyError: No server has that name.
            ValueError: The server's manifest has no ``web_search`` section.
        """
        with self._lock:
            if name is not None:
                entry = next(
                    (e for e in self.entries() if e.manifest.name == name), None
                )
                if entry is None:
                    raise KeyError(f"no MCP server named {name!r} in {self.root}")
                if entry.manifest.web_search is None:
                    raise ValueError(
                        f"{name} has no web_search section in its mcp.yaml"
                    )
            state = self._state()
            state["web_search"] = name
            self._save(state)

    # ----------------------------------------------------------------- what a run uses
    def apply(self, settings: Settings) -> Settings:
        """Return a copy of ``settings`` with the servers switched on here added.

        Enabled, ready servers are appended to ``mcp_servers`` unless one with the same
        name is already configured there. ``web_search`` is bound to the chosen server
        only when that server is enabled, ready and has a ``web_search`` section;
        otherwise it stays as configured.
        """
        entries = self.entries()
        configured = {s.name for s in settings.mcp_servers}
        servers = list(settings.mcp_servers)
        for e in entries:
            if e.enabled and e.ready and e.manifest.name not in configured:
                servers.append(e.server_config())
        web_search = settings.web_search
        chosen = self.web_search_server()
        entry = next((e for e in entries if e.manifest.name == chosen), None)
        if (
            entry is not None
            and entry.enabled
            and entry.ready
            and entry.manifest.web_search is not None
        ):
            spec = entry.manifest.web_search
            web_search = settings.web_search.model_copy(
                update={
                    "enabled": True,
                    "mcp_server": entry.manifest.name,
                    "tool_name": spec.tool_name,
                    "query_arg": spec.query_arg,
                    "limit_arg": spec.limit_arg,
                }
            )
        return settings.model_copy(
            update={"mcp_servers": servers, "web_search": web_search}
        )


def registry(settings: Settings) -> MCPRegistry:
    """Open the registry in the folder that ``settings.mcp_servers_dir`` names."""
    return MCPRegistry(settings.mcp_servers_dir)


def effective(settings: Settings) -> Settings:
    """The settings a run uses, with the servers switched on in mcp_servers/.

    See MCPRegistry.apply.
    """
    return registry(settings).apply(settings)
