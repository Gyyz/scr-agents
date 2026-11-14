"""MCP client: stdio servers from config, exposed as ToolSpecs to the tool loop.

Runs an asyncio loop on a background thread so the synchronous agents can call tools.
Requires the optional `mcp` extra; without it, `available()` is False and nothing
breaks. Tool lists are fetched once per server and tool schemas are flattened
(`normalize_schema`) so local OpenAI-compatible servers, whose tool parsers dislike
`$ref`/`$defs`, accept them. With an activity log attached, every `tools/call` is
recorded as `mcp_start` / `mcp_call` (the server's own reply, before any filtering,
secrets redacted) plus a trace file, and `status()` describes the servers.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import threading
import time
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import Any

from scr_agents.config import MCPServerConfig
from scr_agents.llm.backend import TOOL_CALLER, ToolSpec
from scr_agents.llm.mcp_results import Part, parts

try:  # optional dependency
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    _HAVE_MCP = True
except ImportError:  # pragma: no cover - import guard
    _HAVE_MCP = False


_META_KEYS = {"$schema", "$id", "title", "$defs", "definitions"}
PREVIEW_CHARS = (
    1500  # as much of a reply as the activity log carries (the trace has all of it)
)
_OPAQUE_KEYS = {
    "default",
    "const",
    "enum",
    "examples",
}  # values, not schemas: leave untouched


def normalize_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Copy a JSON schema, inlining local ``$ref`` pointers and dropping metadata keys.

    Unknown or recursive references become a plain object. The input is not mutated.
    """
    root = schema or {}
    defs = {**root.get("definitions", {}), **root.get("$defs", {})}

    def walk(node: Any, seen: frozenset[str]) -> Any:
        if isinstance(node, list):
            return [walk(n, seen) for n in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            target = defs.get(name)
            if target is None or name in seen:
                return {"type": "object"}
            merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return walk(merged, seen | {name})
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _META_KEYS:
                continue
            if key in _OPAQUE_KEYS:
                out[key] = value
            elif key in ("properties", "patternProperties") and isinstance(value, dict):
                out[key] = {name: walk(sub, seen) for name, sub in value.items()}
            else:
                out[key] = walk(value, seen)
        return out

    out: dict[str, Any] = walk(root, frozenset())
    out.setdefault("type", "object")
    return out


class MCPClient:
    """Synchronous access to the tools of the configured stdio MCP servers.

    The sessions live on a private event loop in a background thread, between
    ``start`` and ``close``. A server that fails to start is left out and its error
    kept in ``errors``; the others still work.
    """

    def __init__(
        self,
        servers: list[MCPServerConfig],
        connect_timeout: float = 60.0,
        log: Any | None = None,
        safety: Any | None = None,
    ):
        """Keep the server configs; nothing is started until ``start``.

        Args:
            servers: The servers to connect to; their names must be unique.
            connect_timeout: Seconds each server gets to start, and the default
                timeout of a request to it.
            log: ``ActivityLog`` that records every tool call, or None for no record.
            safety: ``SafetyFilter`` that redacts secrets in replies before they are
                logged, or None to log them as they are.
        """
        self.servers = {s.name: s for s in servers}
        self.connect_timeout = connect_timeout
        # ActivityLog: one mcp_start / mcp_call pair and a trace file per tools/call
        self.log = log
        self.safety = (
            safety  # SafetyFilter: redacts secrets in replies before they are logged
        )
        self.connect_s: dict[str, float] = {}  # how long each server took to start
        self._sessions: dict[str, Any] = {}
        self._tools: dict[str, list[dict[str, Any]]] = {}  # list_tools cache per server
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._main: concurrent.futures.Future[None] | None = (
            None  # the task that owns every session
        )
        self._connected: concurrent.futures.Future[bool] | None = None
        self._stop: asyncio.Event | None = None
        self.errors: dict[str, str] = {}

    @staticmethod
    def available() -> bool:
        """Tell whether the optional ``mcp`` package is installed."""
        return _HAVE_MCP

    def has_server(self, name: str) -> bool:
        """Tell whether the server ``name`` is connected."""
        return name in self._sessions

    # lifecycle -------------------------------------------------------------
    def start(self) -> MCPClient:
        """Start the event loop and connect every server; returns ``self``.

        Blocks until each server has connected or failed. Does nothing without the
        ``mcp`` package, without servers, or when already started.

        Raises:
            TimeoutError: Connecting took longer than ``connect_timeout`` per server
                plus 5 seconds.
        """
        if not _HAVE_MCP or not self.servers or self._loop is not None:
            return self
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="mcp-loop", daemon=True
        )
        self._thread.start()
        self._connected = concurrent.futures.Future()
        self._main = asyncio.run_coroutine_threadsafe(self._serve(), self._loop)
        self._connected.result(
            timeout=self.connect_timeout * max(1, len(self.servers)) + 5
        )
        return self

    def _run(self, coro: Any, timeout: float | None = None) -> Any:
        assert self._loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout or self.connect_timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise

    async def _serve(self) -> None:
        """Enter every server's context managers, serve until close() asks to stop.

        Then exit them, all in this one task (anyio cancel scopes must be exited by the
        task that entered them).
        """
        self._stop = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                for name, cfg in self.servers.items():
                    t0 = time.monotonic()
                    try:
                        async with asyncio.timeout(self.connect_timeout):
                            params = StdioServerParameters(
                                command=cfg.command, args=cfg.args, env=cfg.env or None
                            )
                            read, write = await stack.enter_async_context(
                                stdio_client(params)
                            )
                            session = await stack.enter_async_context(
                                ClientSession(read, write)
                            )
                            await session.initialize()
                        self._sessions[name] = session
                    except Exception as e:
                        self.errors[name] = f"{type(e).__name__}: {e}"
                    self.connect_s[name] = round(time.monotonic() - t0, 3)
                if self._connected is not None and not self._connected.done():
                    self._connected.set_result(True)
                await self._stop.wait()
        except Exception as e:
            self.errors["_serve"] = f"{type(e).__name__}: {e}"
            if self._connected is not None and not self._connected.done():
                self._connected.set_result(False)

    def close(self) -> None:
        """Disconnect every server and stop the event loop; safe to call twice.

        Errors while shutting down are kept in ``errors`` instead of being raised.
        """
        if self._loop is None:
            return
        try:
            if self._stop is not None:
                self._loop.call_soon_threadsafe(self._stop.set)
            if self._main is not None:
                self._main.result(timeout=15)
        except Exception as e:
            self.errors["_close"] = f"{type(e).__name__}: {e}"
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
        self._loop = self._main = self._connected = self._stop = None
        self._sessions.clear()
        self._tools.clear()

    # tools -----------------------------------------------------------------
    def list_tools(self, server: str, refresh: bool = False) -> list[dict[str, Any]]:
        """Tools of one server, fetched once and cached.

        Every agent of a run asks for them.
        """
        if refresh or server not in self._tools:
            result = self._run(self._sessions[server].list_tools())
            self._tools[server] = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": getattr(t, "input_schema", None)
                    or getattr(t, "inputSchema", None)
                    or {"type": "object"},
                }
                for t in result.tools
            ]
        return self._tools[server]

    def status(self) -> list[dict[str, Any]]:
        """Every configured server: connected (with its tools) or failed (and why).

        Names and tools only: commands, arguments and environments can carry keys and
        are never reported.
        """
        out: list[dict[str, Any]] = []
        for name, cfg in self.servers.items():
            entry: dict[str, Any] = {
                "name": name,
                "command": Path(cfg.command).name,
                "untrusted": cfg.untrusted,
                "connect_s": self.connect_s.get(name),
                "tools": [],
            }
            if name in self._sessions:
                entry["status"] = "connected"
                try:
                    entry["tools"] = [
                        {
                            "name": t["name"],
                            "description": " ".join(t["description"].split())[:200],
                        }
                        for t in self.list_tools(name)
                    ]
                except Exception as e:
                    entry["error"] = (
                        f"listing the tools failed: {type(e).__name__}: {e}"
                    )
            else:
                entry["status"] = "failed"
                entry["error"] = (
                    self.errors.get(name) or self.errors.get("_serve") or "not started"
                )
            out.append(entry)
        return out

    def call_tool(self, server: str, name: str, args: dict[str, Any]) -> str:
        """Call tool ``name`` on ``server`` and return the text of its reply.

        The reply may be in any format (JSON, CSV, Markdown, HTML, ...) and hold any
        MCP content type; ``mcp_results.parts`` turns it into text, binary items into
        a summary. The call is logged as ``mcp_start`` and ``mcp_call`` under the agent
        that made it, whether it succeeds or not.

        Raises:
            RuntimeError: The server reported the call as failed.
            KeyError: ``server`` is not connected.
            TimeoutError: No reply within 120 seconds.
        """
        caller = TOOL_CALLER.get() or {}
        agent, call_id = caller.get("agent") or f"mcp:{server}", caller.get("call_id")
        self._log(
            "mcp_start", agent, server=server, tool=name, input=args, call_id=call_id
        )
        cfg = self.servers.get(server)
        t0 = time.monotonic()
        found: list[Part] = []
        is_error, error = False, None
        try:
            result = self._run(
                self._sessions[server].call_tool(name, args), timeout=120
            )
            found = parts(result, cfg.result_format if cfg else None)
            # mcp 2.x names the flag is_error, 1.x isError: missing it reports a failed
            # call as a result
            is_error = bool(
                getattr(result, "is_error", None) or getattr(result, "isError", False)
            )
        except Exception as e:
            is_error, error = True, f"{type(e).__name__}: {e}"
            raise
        finally:
            self._record(
                agent,
                call_id,
                server,
                name,
                args,
                found,
                is_error,
                error,
                time.monotonic() - t0,
            )
        text = "\n".join(p.agent_text for p in found)
        if is_error:
            raise RuntimeError("MCP tool error: " + text)
        return text

    def _log(self, event: str, agent: str, **payload: Any) -> None:
        if self.log is None:
            return
        with suppress(Exception):
            self.log.log(event, agent, **payload)

    def _record(
        self,
        agent: str,
        call_id: str | None,
        server: str,
        tool: str,
        args: dict[str, Any],
        found: list[Part],
        is_error: bool,
        error: str | None,
        duration: float,
    ) -> None:
        """Record the reply as the server sent it, with secrets redacted.

        That is before web-search filtering, HTML-to-text or the untrusted wrapper: a
        preview in `mcp_call`, all of it in the trace, with each part's format.
        """
        if self.log is None:
            return
        items = [p.as_dict() for p in found]
        if self.safety is not None:
            origin = f"mcp:{server}"
            for i in items:
                for key in ("text", "uri"):
                    if i.get(key):
                        i[key] = self.safety.sanitize(i[key], origin).text
        text = "\n".join(i["text"] for i in items)
        formats = {p.format for p in found}
        trace = None
        writer = getattr(self.log, "write_trace", None)
        if writer is not None:
            try:
                trace = writer(
                    f"mcp-{server}-{tool}",
                    {
                        "kind": "mcp",
                        "agent": agent,
                        "server": server,
                        "tool": tool,
                        "call_id": call_id,
                        "duration_s": round(duration, 3),
                        "error": error,
                        "request": {
                            "method": "tools/call",
                            "params": {"name": tool, "arguments": args},
                        },
                        "response": None
                        if error
                        else {"content": items, "isError": is_error},
                    },
                )
            except Exception:
                trace = None
        self._log(
            "mcp_call",
            agent,
            server=server,
            tool=tool,
            input=args,
            call_id=call_id,
            is_error=is_error,
            error=error,
            duration_s=round(duration, 3),
            output_chars=len(text),
            output_preview=text[:PREVIEW_CHARS],
            # one format for the whole reply, "mixed" when its parts differ
            format=formats.pop()
            if len(formats) == 1
            else ("mixed" if formats else None),
            parts=[
                {
                    k: v
                    for k, v in (
                        ("type", p.kind),
                        ("format", p.format),
                        ("mime", p.mime),
                        ("chars", len(p.text)),
                    )
                    if v is not None
                }
                for p in found
            ],
            trace=trace,
        )

    def toolspecs(self, server: str) -> list[ToolSpec]:
        """Wrap the tools of ``server`` as ``ToolSpec`` objects for the tool loop.

        Each is named ``mcp_<server>_<tool>``, takes the flattened input schema and is
        untrusted when the server's config says so. An unconnected server has none.
        """
        if server not in self._sessions:
            return []
        cfg = self.servers[server]
        specs = []
        for t in self.list_tools(server):
            specs.append(
                ToolSpec(
                    name=f"mcp_{server}_{t['name']}",
                    description=f"[MCP {server}] {t['description']}",
                    input_schema=normalize_schema(t["input_schema"]),
                    handler=functools.partial(self.call_tool, server, t["name"]),
                    untrusted=cfg.untrusted,
                    origin=f"mcp:{server}",
                    remote_name=t["name"],
                )
            )
        return specs
