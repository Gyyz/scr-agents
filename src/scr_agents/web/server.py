"""Starlette app: JSON API + server-sent events + the static single-page UI."""

from __future__ import annotations

import hmac
import json
import logging
import queue
import re
import secrets
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import anyio
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request, cookie_parser
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from scr_agents.config import ORCHESTRATORS, Settings
from scr_agents.inputs.project import is_url
from scr_agents.llm.local import LocalBackend
from scr_agents.llm.mcp_registry import COMPONENTS as MCP_COMPONENTS
from scr_agents.llm.mcp_registry import registry as mcp_registry
from scr_agents.models import EvidenceReport
from scr_agents.pipeline import resolve_mode
from scr_agents.sessions import session_dir
from scr_agents.web.chat import ChatManager
from scr_agents.web.runs import FINAL_EVENTS, RunManager
from scr_agents.ws1_review.rounds import RoundError, run_status
from scr_agents.ws3_harness.report_html import (
    csp,
    host_prefixes,
    render_html,
    report_filename,
)
from scr_agents.ws3_harness.report_rounds import render_report_a, render_report_b
from scr_agents.ws4_guardrails.registry import PromptRegistry
from scr_agents.ws5_knowledge.human_review import load_verdicts, record_verdict
from scr_agents.ws5_knowledge.skills import FOLDERS as SKILL_FOLDERS
from scr_agents.ws5_knowledge.skills import LABELS as SKILL_LABELS
from scr_agents.ws5_knowledge.skills import SkillLibrary

STATIC = Path(__file__).parent / "static"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # run and session ids
_TRACE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,160}\.json$")
_NAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$"
)  # MCP server and skill names in URLs
COOKIE = "scr_agents_token"
log = logging.getLogger("scr_agents")


def _sse(rec: dict[str, Any]) -> str:
    return f"data: {json.dumps(rec, default=str)}\n\n"


class NoStaleUI:
    """ASGI middleware that keeps the page and its scripts out of the browser cache.

    The page and its scripts must never come from a stale browser cache: after an update
    the browser kept running the old app.js. `no-cache` makes it revalidate each load (a
    cheap 304 via the ETag).
    """

    def __init__(self, app: Any):
        """Wrap the ASGI ``app``."""
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Pass the request on; ``/`` and ``/static/`` responses get ``no-cache``."""
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path != "/" and not path.startswith("/static/"):
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != b"cache-control"
                ]
                message = {
                    **message,
                    "headers": [*headers, (b"cache-control", b"no-cache")],
                }
            await send(message)

        await self.app(scope, receive, send_with_header)


class ApiGuard:
    """ASGI middleware for ``/api/*``: authentication, JSON bodies, same-origin writes.

    Bearer token or the session cookie, JSON bodies only, and the Origin of a
    state-changing request must be this host (no cross-site POSTs from a web page).
    """

    def __init__(self, app: Any, token: str):
        """Wrap the ASGI ``app``; ``/api/`` requests must present ``token``."""
        self.app = app
        self.token = token

    def _authenticated(self, headers: Headers) -> bool:
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer ") and hmac.compare_digest(
            auth[7:].strip(), self.token
        ):
            return True
        cookie = cookie_parser(headers.get("cookie", "")).get(COOKIE, "")
        return bool(cookie) and hmac.compare_digest(cookie, self.token)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Answer a refused ``/api/`` request with 401, 415 or 403; pass the rest on."""
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        reject: Response | None = None
        if not self._authenticated(headers):
            reject = JSONResponse(
                {
                    "error": "unauthorized: send `Authorization: Bearer <token>` or "
                    "open the console through the URL printed by `scr-agents serve`"
                },
                status_code=401,
            )
        elif scope["method"] in ("POST", "PUT", "PATCH", "DELETE"):
            if (
                not headers.get("content-type", "")
                .lower()
                .startswith("application/json")
            ):
                reject = JSONResponse(
                    {"error": "request body must be application/json"}, status_code=415
                )
            else:
                origin = headers.get("origin")
                if (
                    origin
                    and urlparse(origin).netloc.lower()
                    != headers.get("host", "").lower()
                ):
                    reject = JSONResponse(
                        {"error": "cross-origin requests are not accepted"},
                        status_code=403,
                    )
        if reject is not None:
            await reject(scope, receive, send)
            return
        await self.app(scope, receive, send)


def check_review_target(target: str, settings: Settings) -> str | None:
    """Why the console must not review `target`, or None when it may.

    URLs are limited to the configured schemes; local paths to the clone cache unless
    web.allow_local_paths is set.
    """
    if is_url(target):
        scheme = (
            target.split("://", 1)[0].lower()
            if "://" in target
            else ("ssh" if target.startswith("git@") else "https")
        )
        if scheme not in settings.web.url_schemes:
            return (
                f"URL scheme '{scheme}' is not accepted by the console "
                f"(web.url_schemes: {settings.web.url_schemes})"
            )
        return None
    if settings.web.allow_local_paths:
        return None
    p = Path(target).expanduser().resolve()
    projects = Path(settings.projects_dir).resolve()
    if p == projects or projects in p.parents:
        return None
    return (
        "local paths are not accepted by the console "
        "(set web.allow_local_paths to allow them); give a git URL"
    )


def create_app(
    settings: Settings, token: str | None = None, allowed_hosts: list[str] | None = None
) -> Starlette:
    """Build the console's app: the JSON API, the event streams and the static UI.

    Every ``/api/`` route is behind ``ApiGuard``, and Host headers outside the
    allowlist are refused.

    Args:
        settings: The server's settings, shared by every run and chat.
        token: Secret that API clients and the console must present; defaults to
            ``web.token``, else a random one.
        allowed_hosts: Accepted Host headers; defaults to ``web.allowed_hosts``.

    Returns:
        The app, with its run manager, chat manager and token on ``app.state``.
    """
    manager = RunManager(settings)
    chats = ChatManager(settings, manager)
    token = token or settings.web.token or secrets.token_urlsafe(24)

    async def index(request: Request) -> Response:
        given = request.query_params.get("token")
        if given is not None:
            # the printed URL: exchange the token for a SameSite cookie, then drop it
            # from the URL
            if not hmac.compare_digest(given, token):
                return JSONResponse({"error": "wrong token"}, status_code=401)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/")
            return resp
        return FileResponse(STATIC / "index.html")

    async def api_config(_: Request) -> Response:
        cfg = settings.llm.local
        reachable = await anyio.to_thread.run_sync(
            lambda: LocalBackend.reachable(cfg.base_url, cfg.api_key)
        )
        served: list[str] = []
        if reachable:
            try:
                served = await anyio.to_thread.run_sync(
                    lambda: LocalBackend.list_models(cfg.base_url, cfg.api_key)
                )
            except Exception:
                served = []
        mode = await anyio.to_thread.run_sync(lambda: resolve_mode(settings))
        live = await anyio.to_thread.run_sync(
            lambda: mcp_registry(settings).apply(settings)
        )  # what a run would use
        return JSONResponse(
            {
                "mode": mode,
                "configured_mode": settings.llm.mode,
                "orchestrator": settings.review.orchestrator,
                "orchestrators": list(ORCHESTRATORS),
                "local": {
                    "base_url": cfg.base_url,
                    "reachable": reachable,
                    "models": served,
                    "model": cfg.model or (served[0] if served else ""),
                    "validator_model": cfg.validator_model
                    or cfg.model
                    or (served[0] if served else ""),
                },
                "reviewer_model": settings.llm.reviewer_model,
                "validator_model": settings.llm.validator_model,
                "output_dir": str(Path(settings.output_dir).resolve()),
                "session_id": manager.session_id,
                "scanners": settings.scanners.enabled,
                "web_search": live.web_search.enabled,
                "mcp_servers": [s.name for s in live.mcp_servers],
            }
        )

    # ------------------------------------------------- settings: MCP servers and skills
    # The console switches things on and off; what a server runs comes from
    # mcp_servers/<server>/mcp.yaml on disk.
    def mcp_listing() -> dict[str, Any]:
        reg = mcp_registry(settings)
        entries = reg.entries()
        live = reg.apply(settings)
        servers = [e.summary() for e in entries]
        names = {e.manifest.name for e in entries}
        for (
            s
        ) in settings.mcp_servers:  # from scr_agents.yaml: always on, shown read-only
            servers.append(
                {
                    "name": s.name,
                    "folder": None,
                    "description": "defined in the config file (mcp_servers)",
                    "command": [s.command, *s.args],
                    "env_keys": sorted(s.env),
                    "untrusted": s.untrusted,
                    "enabled": True,
                    "components": s.components,
                    "ready": True,
                    "missing": [],
                    "install": "",
                    "web_search": None,
                    "source": "config",
                    "shadows": s.name in names,
                }
            )
        ws = live.web_search
        return {
            "dir": str(reg.root.resolve()),
            "servers": servers,
            "components": MCP_COMPONENTS,
            "web_search": {
                "chosen": reg.web_search_server(),
                "enabled": ws.enabled,
                "server": ws.mcp_server,
                "tool": ws.tool_name,
                "allowlist": ws.allowlist,
            },
            "errors": reg.errors,
        }

    async def api_settings_mcp_servers(_: Request) -> Response:
        return JSONResponse(await anyio.to_thread.run_sync(mcp_listing))

    async def api_settings_mcp(request: Request) -> Response:
        name, body = request.path_params["name"], await request.json()
        if not _NAME.match(name) or not isinstance(body, dict):
            return JSONResponse({"error": "bad request"}, status_code=400)
        comps = body.get("components")
        if comps is not None and (
            not isinstance(comps, list) or not all(isinstance(c, str) for c in comps)
        ):
            return JSONResponse(
                {"error": "components must be a list of names"}, status_code=400
            )
        enabled = body.get("enabled")
        try:
            await anyio.to_thread.run_sync(
                lambda: mcp_registry(settings).set_server(
                    name,
                    enabled=None if enabled is None else bool(enabled),
                    components=comps,
                )
            )
        except KeyError as e:
            return JSONResponse({"error": str(e).strip("'\"")}, status_code=404)
        except (ValueError, OSError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        log.info("MCP server %s: enabled=%s components=%s", name, enabled, comps)
        return JSONResponse(await anyio.to_thread.run_sync(mcp_listing))

    async def api_settings_web_search(request: Request) -> Response:
        body = await request.json()
        server = body.get("server") if isinstance(body, dict) else None
        if server is not None and (
            not isinstance(server, str) or not _NAME.match(server)
        ):
            return JSONResponse(
                {"error": "server must be a server name or null"}, status_code=400
            )
        try:
            await anyio.to_thread.run_sync(
                lambda: mcp_registry(settings).set_web_search(server)
            )
        except KeyError as e:
            return JSONResponse({"error": str(e).strip("'\"")}, status_code=404)
        except (ValueError, OSError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(await anyio.to_thread.run_sync(mcp_listing))

    async def api_settings_mcp_test(request: Request) -> Response:
        """Start one server (as a review would), list its tools, stop it."""
        name = request.path_params["name"]
        if not _NAME.match(name):
            return JSONResponse({"error": "bad server name"}, status_code=400)

        def test() -> dict[str, Any]:
            from scr_agents.llm.mcp_client import MCPClient

            entry = next(
                (
                    e
                    for e in mcp_registry(settings).entries()
                    if e.manifest.name == name
                ),
                None,
            )
            cfg = (
                entry.server_config()
                if entry is not None
                else next((s for s in settings.mcp_servers if s.name == name), None)
            )
            if cfg is None:
                return {
                    "ok": False,
                    "error": f"no MCP server named {name!r}",
                    "status": 404,
                }
            if entry is not None and not entry.ready:
                return {
                    "ok": False,
                    "error": "not ready: " + "; ".join(entry.missing),
                    "install": entry.manifest.install,
                }
            if not MCPClient.available():
                return {
                    "ok": False,
                    "error": "the `mcp` package is not installed (uv sync --extra mcp)",
                }
            t0 = time.monotonic()
            client = MCPClient([cfg], connect_timeout=30).start()
            try:
                if name in client.errors or not client.has_server(name):
                    return {
                        "ok": False,
                        "error": client.errors.get(name)
                        or client.errors.get("_serve")
                        or "did not start",
                        "duration_s": round(time.monotonic() - t0, 2),
                    }
                tools = [
                    {
                        "name": t["name"],
                        "description": " ".join(t["description"].split())[:200],
                    }
                    for t in client.list_tools(name)
                ]
                return {
                    "ok": True,
                    "tools": tools,
                    "duration_s": round(time.monotonic() - t0, 2),
                }
            finally:
                client.close()

        out = await anyio.to_thread.run_sync(test)
        return JSONResponse(out, status_code=out.pop("status", 200))

    def skills_listing() -> dict[str, Any]:
        lib = SkillLibrary(settings.skill_paths())
        folders = []
        for folder, component in SKILL_FOLDERS.items():
            folders.append(
                {
                    "folder": folder,
                    "component": component,
                    "label": SKILL_LABELS[component],
                    "skills": [
                        {**s.summary(), "chars": len(s.body)}
                        for s in lib.skills
                        if s.component == component
                    ],
                }
            )
        return {
            "paths": [str(p.resolve()) for p in lib.paths],
            "folders": folders,
            "errors": lib.errors,
        }

    async def api_settings_skills(_: Request) -> Response:
        return JSONResponse(await anyio.to_thread.run_sync(skills_listing))

    async def api_settings_skill(request: Request) -> Response:
        """GET: one skill with its text; POST {enabled}: switch it on or off."""
        name = request.path_params["name"]
        if not _NAME.match(name):
            return JSONResponse({"error": "bad skill name"}, status_code=400)
        lib = await anyio.to_thread.run_sync(
            lambda: SkillLibrary(settings.skill_paths())
        )
        skill = next((s for s in lib.skills if s.name == name), None)
        if skill is None:
            return JSONResponse({"error": f"no skill named {name!r}"}, status_code=404)
        if request.method == "GET":
            return JSONResponse({**skill.summary(), "body": skill.body})
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            return JSONResponse(
                {"error": 'send {"enabled": true|false}'}, status_code=400
            )
        try:
            await anyio.to_thread.run_sync(
                lambda: lib.set_enabled(name, body["enabled"])
            )
        except OSError as e:
            return JSONResponse(
                {"error": f"could not write {skill.root}/config.yaml: {e}"},
                status_code=500,
            )
        log.info("skill %s: enabled=%s", name, body["enabled"])
        return JSONResponse(await anyio.to_thread.run_sync(skills_listing))

    async def api_runs(_: Request) -> Response:
        return JSONResponse(await anyio.to_thread.run_sync(manager.list_runs))

    async def api_sessions(_: Request) -> Response:
        return JSONResponse(await anyio.to_thread.run_sync(manager.sessions))

    async def api_review(request: Request) -> Response:
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must be a JSON object"}, status_code=400
            )
        target = str(body.get("repo") or "")
        problem = (
            check_review_target(target, settings) if target else "repo is required"
        )
        if problem:
            return JSONResponse({"error": problem}, status_code=400)
        # the LLM mode is the server's choice (config / SCR_AGENTS_MODE), never the
        # client's
        try:
            run = await anyio.to_thread.run_sync(
                lambda: manager.start(
                    repo=target,
                    base=body["base"],
                    head=body.get("head") or "HEAD",
                    pr=body.get("pr"),
                    task=body.get("task") or "find_bugs",
                    claim=body.get("claim"),
                    instructions=body.get("instructions"),
                    scope=body.get("scope") or "range",
                    orchestrator=body.get("orchestrator") or None,
                )
            )
        except (KeyError, ValueError, RuntimeError, OSError) as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
        return JSONResponse(run.summary(), status_code=201)

    async def api_demo(_: Request) -> Response:
        run = await anyio.to_thread.run_sync(manager.start_demo)
        return JSONResponse(run.summary(), status_code=201)

    async def api_run(request: Request) -> Response:
        s = manager.summary(request.path_params["run_id"])
        if s is None:
            return JSONResponse({"error": "unknown run"}, status_code=404)
        return JSONResponse(s)

    async def api_events(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        live = manager.get(run_id)

        async def gen() -> AsyncIterator[str]:
            if live is None:
                events = manager.disk_events(run_id)
                if events is None:
                    yield _sse({"event": "error", "error": "unknown run"})
                    return
                for e in events:
                    yield _sse(e)
                s = manager.disk_summary(run_id) or {}
                yield _sse(
                    {
                        "event": "run_finished"
                        if s.get("status") == "finished"
                        else "run_failed",
                        "counts": s.get("counts"),
                        "run_dir": s.get("run_dir"),
                        "replay": True,
                    }
                )
                return
            snapshot, q = live.subscribe()
            try:
                for e in snapshot:
                    yield _sse(e)
                if snapshot and snapshot[-1].get("event") in FINAL_EVENTS:
                    return
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        e = await anyio.to_thread.run_sync(q.get, True, 1.0)
                    except queue.Empty:
                        if live.status != "running" and q.empty():
                            return
                        yield ": keepalive\n\n"
                        continue
                    yield _sse(e)
                    if e.get("event") in FINAL_EVENTS:
                        return
            finally:
                live.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------ chat
    async def api_chats(_: Request) -> Response:
        return JSONResponse(await anyio.to_thread.run_sync(chats.list))

    async def api_chat_create(request: Request) -> Response:
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must be a JSON object"}, status_code=400
            )
        text = str(body.get("text") or "").strip()
        orchestrator = body.get("orchestrator") or None
        if orchestrator is not None and orchestrator not in ORCHESTRATORS:
            return JSONResponse(
                {"error": f"orchestrator must be one of {list(ORCHESTRATORS)}"},
                status_code=400,
            )
        try:
            if body.get(
                "demo"
            ):  # a repository the server generates itself: no client path involved
                from scr_agents.demo import make_demo_repo

                def _demo() -> tuple[Path, str]:
                    import tempfile

                    dest = Path(tempfile.mkdtemp(prefix="scr-agents-demo-"))
                    make_demo_repo(dest)
                    return dest, "demo"

                repo, target = await anyio.to_thread.run_sync(_demo)
            else:
                target = str(body.get("repo") or "").strip()
                problem = (
                    check_review_target(target, settings)
                    if target
                    else "repo is required (a git URL)"
                )
                if problem:
                    return JSONResponse({"error": problem}, status_code=400)
                # cloned on the chat's thread: Start answers at once, progress shows in
                # the chat
                repo = None
            chat = await anyio.to_thread.run_sync(
                lambda: chats.create(target, repo, text or None, orchestrator)
            )
        except (ValueError, RuntimeError, OSError) as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
        return JSONResponse(chat.summary(), status_code=201)

    async def api_chat(request: Request) -> Response:
        chat = chats.get(request.path_params["chat_id"], restore=False)
        if chat is not None:
            return JSONResponse(chat.summary())
        listed = next(
            (c for c in chats.list() if c["chat_id"] == request.path_params["chat_id"]),
            None,
        )
        return (
            JSONResponse(listed)
            if listed
            else JSONResponse({"error": "unknown chat"}, status_code=404)
        )

    async def api_chat_message(request: Request) -> Response:
        body = await request.json()
        text = (
            str((body or {}).get("text") or "").strip()
            if isinstance(body, dict)
            else ""
        )
        if not text:
            return JSONResponse({"error": "text is required"}, status_code=400)
        chat = await anyio.to_thread.run_sync(
            lambda: chats.get(request.path_params["chat_id"])
        )
        if chat is None:
            return JSONResponse(
                {"error": "unknown chat (or its repository is gone)"}, status_code=404
            )
        chat.send(text)
        return JSONResponse({"queued": True, "busy": chat.busy}, status_code=202)

    async def api_chat_events(request: Request) -> Response:
        chat_id = request.path_params["chat_id"]
        chat = chats.get(chat_id, restore=False)
        follow = (
            request.query_params.get("follow", "1") != "0"
        )  # follow=0: the transcript so far, then close

        async def gen() -> AsyncIterator[str]:
            if chat is None:
                records = chats.records(chat_id)
                if records is None:
                    yield _sse({"event": "error", "error": "unknown chat"})
                    return
                for r in records:
                    yield _sse(r)
                yield _sse({"event": "replay_done", "live": False})
                return
            snapshot, q = chat.feed.subscribe()
            try:
                for r in snapshot:
                    yield _sse(r)
                yield _sse({"event": "replay_done", "live": True, "busy": chat.busy})
                while follow:
                    if await request.is_disconnected():
                        return
                    try:
                        r = await anyio.to_thread.run_sync(q.get, True, 1.0)
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    yield _sse(r)
            finally:
                chat.feed.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _trace_response(base: Path, name: str) -> Response:
        """One trace file of a run or a chat.

        Names are what events carry (`trace/<file>.json`).
        """
        name = name.removeprefix("trace/")
        if not _TRACE_NAME.match(name):
            return JSONResponse({"error": "bad trace name"}, status_code=400)
        trace_dir = (base / "trace").resolve()
        target = (trace_dir / name).resolve()
        if target.parent != trace_dir or not target.is_file():
            return JSONResponse({"error": "no such trace"}, status_code=404)
        return FileResponse(target, media_type="application/json")

    async def api_run_trace(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        if not _ID.match(run_id):
            return JSONResponse({"error": "bad run id"}, status_code=400)
        return _trace_response(manager.run_dir(run_id), request.path_params["name"])

    async def api_chat_trace(request: Request) -> Response:
        chat_id = request.path_params["chat_id"]
        if not _ID.match(chat_id) or chats.records(chat_id) is None:
            return JSONResponse({"error": "unknown chat"}, status_code=404)
        return _trace_response(
            session_dir(settings, chat_id), request.path_params["name"]
        )

    async def api_chat_delete(request: Request) -> Response:
        from scr_agents.sessions import DeleteError
        from scr_agents.web.chat import ChatBusyError

        chat_id = request.path_params["chat_id"]
        if not _ID.match(chat_id):
            return JSONResponse({"error": "bad conversation id"}, status_code=400)
        try:
            out = await anyio.to_thread.run_sync(lambda: chats.delete(chat_id))
        except ChatBusyError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except DeleteError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        return JSONResponse({"deleted": True, **out})

    async def api_run_delete(request: Request) -> Response:
        from scr_agents.sessions import DeleteError
        from scr_agents.web.runs import RunBusyError

        run_id = request.path_params["run_id"]
        if not _ID.match(run_id):
            return JSONResponse({"error": "bad run id"}, status_code=400)
        try:
            out = await anyio.to_thread.run_sync(lambda: manager.delete(run_id))
        except RunBusyError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except DeleteError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        return JSONResponse({"deleted": True, **out})

    def report_page(run_id: str, download: bool) -> Response | None:
        """The HTML report of a run, or None while it has no report.json.

        Rendered now from report.json and the human verdicts recorded since. A download
        is meant for people who are not on this machine, so it leaves out host paths.
        """
        d = manager.run_dir(run_id)
        if not (d / "report.json").is_file():
            return None
        report = EvidenceReport.model_validate_json((d / "report.json").read_text())
        nonce = secrets.token_urlsafe(16)
        headers = {
            "content-security-policy": csp(nonce),
            "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer",
            "x-frame-options": "DENY",
            "cache-control": "no-store",
        }
        if download:
            headers["content-disposition"] = (
                f'attachment; filename="{report_filename(report)}"'
            )
        scrub = (
            host_prefixes(report, str(d), str(Path(settings.output_dir).resolve()))
            if download
            else None
        )
        return HTMLResponse(
            render_html(
                report, load_verdicts(d), shared=download, nonce=nonce, scrub=scrub
            ),
            headers=headers,
        )

    def round_page(run_id: str, page: str) -> Response | None:
        """The Review Report or the Validator Report of a run's round, or None.

        Rendered now from report.json and the human verdicts recorded since, so
        Validator Report shows which findings are settled at this moment.
        """
        d = manager.run_dir(run_id)
        if not (d / "report.json").is_file():
            return None
        report = EvidenceReport.model_validate_json((d / "report.json").read_text())
        nonce = secrets.token_urlsafe(16)
        html = (
            render_report_a(report, load_verdicts(d), nonce=nonce)
            if page == "a"
            else render_report_b(
                report,
                load_verdicts(d),
                nonce=nonce,
                max_rounds=settings.review.max_rounds,
            )
        )
        return HTMLResponse(
            html,
            headers={
                "content-security-policy": csp(nonce),
                "x-content-type-options": "nosniff",
                "referrer-policy": "no-referrer",
                "x-frame-options": "DENY",
                "cache-control": "no-store",
            },
        )

    async def api_round_page(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        if not _ID.match(run_id):
            return JSONResponse({"error": "bad run id"}, status_code=400)
        page = "a" if request.url.path.endswith("report_a.html") else "b"
        resp = await anyio.to_thread.run_sync(lambda: round_page(run_id, page))
        return resp or JSONResponse({"error": "no report yet"}, status_code=404)

    async def api_round(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        if not _ID.match(run_id):
            return JSONResponse({"error": "bad run id"}, status_code=400)
        d = manager.run_dir(run_id)
        if not (d / "report.json").is_file():
            return JSONResponse({"error": "no report yet"}, status_code=404)
        st = await anyio.to_thread.run_sync(
            lambda: run_status(d, settings.review.max_rounds)
        )
        return JSONResponse(st.model_dump())

    async def api_next_round(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        if not _ID.match(run_id):
            return JSONResponse({"error": "bad run id"}, status_code=400)
        d = manager.run_dir(run_id)
        if not (d / "report.json").is_file():
            return JSONResponse({"error": "no report yet"}, status_code=404)
        try:
            run = await anyio.to_thread.run_sync(lambda: manager.start_next_round(d))
        except (RoundError, ValueError, OSError) as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse(run.summary(), status_code=201)

    async def api_report_html(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        if not _ID.match(run_id):
            return JSONResponse({"error": "bad run id"}, status_code=400)
        download = request.query_params.get("download") == "1"
        page = await anyio.to_thread.run_sync(lambda: report_page(run_id, download))
        return page or JSONResponse({"error": "no report yet"}, status_code=404)

    async def api_report(request: Request) -> Response:
        if not _ID.match(request.path_params["run_id"]):
            return JSONResponse({"error": "bad run id"}, status_code=400)
        d = manager.run_dir(request.path_params["run_id"])
        if not (d / "report.json").exists():
            return JSONResponse({"error": "no report yet"}, status_code=404)
        return JSONResponse(
            {
                "report": json.loads((d / "report.json").read_text()),
                "markdown": (d / "report.md").read_text()
                if (d / "report.md").exists()
                else "",
            }
        )

    async def api_verdict(request: Request) -> Response:
        d = manager.run_dir(request.path_params["run_id"])
        body = await request.json()
        registry = PromptRegistry(
            settings.manifest_path(),
            settings.registry.require_approved,
            stats_path=Path(settings.output_dir).resolve() / "registry_stats.jsonl",
        )
        try:
            rec = await anyio.to_thread.run_sync(
                lambda: record_verdict(
                    d,
                    body["finding"],
                    body["verdict"],
                    body.get("note") or "",
                    registry,
                )
            )
        except (KeyError, ValueError, OSError) as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
        return JSONResponse(rec)

    hosts = allowed_hosts or settings.web.allowed_hosts
    app = Starlette(
        middleware=[
            Middleware(TrustedHostMiddleware, allowed_hosts=hosts),
            Middleware(NoStaleUI),
            Middleware(ApiGuard, token=token),
        ],
        routes=[
            Route("/", index),
            Route("/api/config", api_config),
            Route("/api/runs", api_runs),
            Route("/api/sessions", api_sessions),
            Route("/api/review", api_review, methods=["POST"]),
            Route("/api/demo", api_demo, methods=["POST"]),
            Route("/api/runs/{run_id}", api_run, methods=["GET"]),
            Route("/api/runs/{run_id}", api_run_delete, methods=["DELETE"]),
            Route("/api/runs/{run_id}/events", api_events),
            Route("/api/runs/{run_id}/report", api_report),
            Route("/api/runs/{run_id}/report.html", api_report_html),
            Route("/api/runs/{run_id}/report_a.html", api_round_page),
            Route("/api/runs/{run_id}/report_b.html", api_round_page),
            Route("/api/runs/{run_id}/round", api_round),
            Route("/api/runs/{run_id}/next-round", api_next_round, methods=["POST"]),
            Route("/api/runs/{run_id}/verdict", api_verdict, methods=["POST"]),
            Route("/api/chats", api_chats),
            Route("/api/chats/new", api_chat_create, methods=["POST"]),
            Route("/api/chats/{chat_id}", api_chat, methods=["GET"]),
            Route("/api/chats/{chat_id}", api_chat_delete, methods=["DELETE"]),
            Route("/api/chats/{chat_id}/events", api_chat_events),
            Route("/api/chats/{chat_id}/messages", api_chat_message, methods=["POST"]),
            Route("/api/runs/{run_id}/trace/{name:path}", api_run_trace),
            Route("/api/chats/{chat_id}/trace/{name:path}", api_chat_trace),
            Route("/api/settings/mcp-servers", api_settings_mcp_servers),
            Route(
                "/api/settings/mcp-servers/{name}", api_settings_mcp, methods=["POST"]
            ),
            Route(
                "/api/settings/mcp-servers/{name}/test",
                api_settings_mcp_test,
                methods=["POST"],
            ),
            Route(
                "/api/settings/web-search", api_settings_web_search, methods=["POST"]
            ),
            Route("/api/settings/skills", api_settings_skills),
            Route(
                "/api/settings/skills/{name}",
                api_settings_skill,
                methods=["GET", "POST"],
            ),
            Mount("/static", StaticFiles(directory=str(STATIC)), name="static"),
        ],
    )
    app.state.manager = manager
    app.state.chats = chats
    app.state.token = token
    return app


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the web console with uvicorn until it is stopped.

    Prints the console URL with its token. Bound to a host other than loopback
    with the default ``web.allowed_hosts``, it accepts any Host header and logs a
    warning to set that option.

    Args:
        settings: The server's settings.
        host: Address to bind.
        port: Port to listen on.
    """
    import uvicorn

    token = settings.web.token or secrets.token_urlsafe(24)
    hosts = list(settings.web.allowed_hosts)
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not loopback and hosts == ["localhost", "127.0.0.1", "[::1]"]:
        hosts = ["*"]
        log.warning(
            "serving on %s with the default web.allowed_hosts; "
            "accepting any Host header. Set "
            "web.allowed_hosts to the console's hostname to block DNS rebinding.",
            host,
        )
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"SCR Agents console: http://{shown}:{port}/?token={token}")
    print(
        f"API clients: Authorization: Bearer {token}   "
        f"(output dir: {Path(settings.output_dir).resolve()})"
    )
    if not settings.web.token:
        print(
            "the token is generated per start; "
            "set web.token or SCR_AGENTS_TOKEN to keep it stable"
        )
    uvicorn.run(
        create_app(settings, token=token, allowed_hosts=hosts),
        host=host,
        port=port,
        log_level="info",
    )
