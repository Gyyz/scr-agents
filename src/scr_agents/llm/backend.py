"""LLM backends.

`AnthropicBackend` calls the Claude API through the official SDK. `OfflineBackend` is a
deterministic stand-in that drives the same tool loop without a network so the whole
pipeline can run in CI and tests. Both speak the same minimal content-block dialect
(dicts shaped like the Messages API: text / tool_use / tool_result).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast


class BackendError(RuntimeError):
    """A failed model call.

    `exchanges` holds what was sent and received, for the trace.
    """

    def __init__(self, message: str, exchanges: list[dict[str, Any]] | None = None):
        """Keep a copy of ``exchanges``, the API traffic of the failed call."""
        super().__init__(message)
        self.exchanges: list[dict[str, Any]] = list(exchanges or [])


# The tool call a handler is running for: {"agent": ..., "call_id": ...}, set by the
# tool loop around each handler, so a handler that calls out (the MCP client) can say
# which agent's call it serves.
TOOL_CALLER: ContextVar[dict[str, Any] | None] = ContextVar("tool_caller", default=None)


@dataclass
class ToolSpec:
    """A tool offered to the model: its API definition and the handler that runs it.

    The flags tell the tool loop how to treat a call: ``untrusted`` results pass
    through the safety filter, and tools that are not ``parallel_safe`` run one at a
    time across the loops that share a lock.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    untrusted: bool = False  # result passes through the safety filter
    parallel_safe: bool = True
    # where the tool runs: "" for built-in tools, "mcp:<server>" for MCP tools (shown
    # and logged with each call)
    origin: str = ""
    remote_name: str = (
        ""  # the tool's own name on that server (MCP), without the mcp_<server>_ prefix
    )

    def to_api(self) -> dict[str, Any]:
        """Return the tool definition sent to the model, typed as an object schema."""
        schema = dict(self.input_schema)
        schema.setdefault("type", "object")
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
        }


@dataclass
class BackendResponse:
    """One model turn as Messages API content blocks, with usage and the API traffic."""

    content: list[dict[str, Any]]
    stop_reason: str
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    stop_details: dict[str, Any] | None = None
    raw_content: Any = (
        None  # provider-native blocks to replay on the next turn, when available
    )
    # the API traffic behind this response: one entry per HTTP request (retries and
    # repair calls included), with the exact request body and response; credentials are
    # never part of it
    exchanges: list[dict[str, Any]] = field(default_factory=list)

    def text(self) -> str:
        """Return the text blocks of the turn, joined."""
        return "".join(
            b.get("text", "") for b in self.content if b.get("type") == "text"
        )

    def tool_uses(self) -> list[dict[str, Any]]:
        """Return the ``tool_use`` blocks of the turn, in order."""
        return [b for b in self.content if b.get("type") == "tool_use"]


class LLMBackend(Protocol):
    """What the tool loop needs from a model: one ``complete`` call per turn."""

    name: str
    model: str

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        output_schema: dict[str, Any] | None = None,
        max_tokens: int = 16000,
    ) -> BackendResponse:
        """Run one model turn on the conversation so far.

        Args:
            system: The system prompt.
            messages: The conversation, as Messages API messages with content blocks.
            tools: Tool definitions, as ``ToolSpec.to_api`` returns them.
            output_schema: JSON schema the final answer must follow, if any.
            max_tokens: Upper bound on the tokens generated in this turn.

        Raises:
            BackendError: The call failed; its ``exchanges`` hold the traffic.
        """
        ...


def has_anthropic_credentials() -> bool:
    """Tell whether the Anthropic SDK would find credentials.

    Either ``ANTHROPIC_API_KEY`` or ``ANTHROPIC_AUTH_TOKEN`` is set, or the
    ``anthropic`` folder in the user's config directory is not empty.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    profile_dir = (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "anthropic"
    )
    return profile_dir.exists() and any(profile_dir.iterdir())


# --------------------------------------------------------------------------- Anthropic


class AnthropicBackend:
    """Calls the Claude API through the official SDK; used only with mode anthropic."""

    name = "anthropic"
    context_window = 200_000

    def __init__(
        self,
        model: str,
        effort: str = "high",
        server_fallbacks: bool = True,
        thinking_display: str = "omitted",
        client: Any | None = None,
        timeout: float = 600.0,
    ):
        """Create the SDK client, unless one is passed in.

        Args:
            model: The Claude model ID.
            effort: The ``output_config`` effort level of every request.
            server_fallbacks: Turn on the API's server-side refusal fallbacks.
            thinking_display: ``"summarized"`` asks for summarised adaptive thinking;
                ``"omitted"`` leaves thinking out of the request.
            client: A ready ``anthropic.Anthropic`` client, for tests.
            timeout: Request timeout of the client created here, in seconds.
        """
        # imported lazily so offline mode never needs the package configured
        import anthropic

        self._anthropic = anthropic
        self.model = model
        self.effort = effort
        self.server_fallbacks = server_fallbacks
        self.thinking_display = thinking_display
        self.client = client or anthropic.Anthropic(timeout=timeout)

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        output_schema: dict[str, Any] | None = None,
        max_tokens: int = 16000,
    ) -> BackendResponse:
        """Send one turn to the Claude API, with the system prompt marked for caching.

        Raises:
            BackendError: The API rejected or failed the call; the message says how
                (bad request, authentication, unknown model, rate limit, other status
                or unreachable).
        """
        a = self._anthropic
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": messages,
            "output_config": {"effort": self.effort},
        }
        if tools:
            kwargs["tools"] = tools
        if output_schema:
            kwargs["output_config"]["format"] = {
                "type": "json_schema",
                "schema": output_schema,
            }
        if self.thinking_display == "summarized":
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
        if self.server_fallbacks:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        exchange: dict[str, Any] = {
            "api": "anthropic",
            "endpoint": "beta.messages.create",
            "request": kwargs,
        }
        t0 = time.monotonic()
        try:
            resp = self.client.beta.messages.create(**kwargs)
        except a.APIError as e:
            exchange.update(
                error=f"{type(e).__name__}: {getattr(e, 'message', e)}",
                status=getattr(e, "status_code", None),
                duration_s=round(time.monotonic() - t0, 3),
            )
            ex = [exchange]
            if isinstance(e, a.BadRequestError):
                raise BackendError(
                    f"bad request to {self.model}: {e.message}", ex
                ) from e
            if isinstance(e, a.AuthenticationError):
                raise BackendError(
                    "Anthropic authentication failed (check ANTHROPIC_API_KEY)", ex
                ) from e
            if isinstance(e, a.NotFoundError):
                raise BackendError(
                    f"model not found: {self.model}: {e.message}", ex
                ) from e
            if isinstance(e, a.RateLimitError):
                raise BackendError(
                    f"rate limited by Anthropic after SDK retries: {e.message}", ex
                ) from e
            if isinstance(e, a.APIStatusError):
                raise BackendError(
                    f"Anthropic API error {e.status_code}: {e.message}", ex
                ) from e
            raise BackendError(f"could not reach the Anthropic API: {e}", ex) from e
        exchange.update(
            status=200,
            duration_s=round(time.monotonic() - t0, 3),
            response=resp.model_dump(mode="json", exclude_none=True),
            request_id=getattr(resp, "_request_id", None),
        )

        content = [b.model_dump(mode="json", exclude_none=True) for b in resp.content]
        usage = {
            "input_tokens": getattr(resp.usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(resp.usage, "output_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0)
            or 0,
            "cache_creation_input_tokens": getattr(
                resp.usage, "cache_creation_input_tokens", 0
            )
            or 0,
        }
        stop_details = None
        if resp.stop_reason == "refusal" and getattr(resp, "stop_details", None):
            stop_details = resp.stop_details.model_dump(mode="json")
        return BackendResponse(
            content=content,
            stop_reason=resp.stop_reason or "end_turn",
            usage=usage,
            model=resp.model,
            stop_details=stop_details,
            raw_content=resp.content,
            exchanges=[exchange],
        )


# --------------------------------------------------------------------------- Offline

_FENCED_JSON = re.compile(r"```json\s*(\{[\s\S]*?\})\s*```")


class OfflineBackend:
    """Deterministic model stand-in.

    Policies by role::

      planner   -> returns a trivial plan.
      reviewer  -> calls `scanner_findings`, then converts scanner results in its focus
                   category into findings. The vulnerability reviewer first looks the
                   first hit's CWE up with `web_search` when that tool is offered (an
                   MCP server is configured).
      validator -> calls `run_tests` on head, then confirms scanner-backed findings and
                   leaves everything else unverified.
      orchestrator -> (OpenFlow) runs the bugs, vulnerabilities and style reviewers
                   one after another, lists the candidates, validates all of them and
                   reports all of them.
      lead      -> starts the review or the next round the message asks for, then
                   summarises the result.
    """

    name = "offline"

    def __init__(self, role: str, focus: str | None = None):
        """Pick the policy for ``role``.

        One of planner, reviewer, validator, orchestrator or lead.

        ``focus`` is the reviewer's category, such as ``"vulnerability"``; None keeps
        scanner results of every category.
        """
        self.role = role
        self.focus = focus
        self.model = f"offline-{role}"
        self._n = 0

    # helpers ---------------------------------------------------------------
    @staticmethod
    def _called(messages: list[dict[str, Any]]) -> dict[str, str]:
        """Map tool name -> last result text."""
        ids: dict[str, str] = {}
        out: dict[str, str] = {}
        for m in messages:
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if b.get("type") == "tool_use":
                    ids[b["id"]] = b["name"]
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in ids:
                    c = b.get("content")
                    if isinstance(c, list):
                        c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
                    out[ids[b["tool_use_id"]]] = str(c or "")
        return out

    @staticmethod
    def _first_user_text(messages: list[dict[str, Any]]) -> str:
        for m in messages:
            if m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return "".join(b.get("text", "") for b in c if isinstance(b, dict))
        return ""

    @staticmethod
    def _json_in(text: str) -> dict[str, Any] | None:
        for m in _FENCED_JSON.finditer(text):
            try:
                return cast("dict[str, Any] | None", json.loads(m.group(1)))
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _strip_untrusted(text: str) -> str:
        m = re.search(r"<untrusted[^>]*>\n?([\s\S]*?)\n?</untrusted>", text)
        return m.group(1) if m else text

    def _tool_use(self, name: str, inp: dict[str, Any]) -> BackendResponse:
        self._n += 1
        call_id = (
            f"toolu_offline_{uuid.uuid4().hex[:8]}_{self._n}"  # unique across agents
        )
        return BackendResponse(
            content=[{"type": "tool_use", "id": call_id, "name": name, "input": inp}],
            stop_reason="tool_use",
            model=self.model,
        )

    def _final(self, data: dict[str, Any]) -> BackendResponse:
        return BackendResponse(
            content=[{"type": "text", "text": json.dumps(data)}],
            stop_reason="end_turn",
            model=self.model,
        )

    # policy ----------------------------------------------------------------
    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        output_schema: dict[str, Any] | None = None,
        max_tokens: int = 16000,
    ) -> BackendResponse:
        """Return the next step of the role's fixed policy for this conversation.

        Raises:
            BackendError: The role has no offline policy.
        """
        names = {t["name"] for t in tools}
        called = self._called(messages)
        if self.role == "planner":
            return self._final(
                {
                    "focus_files": [],
                    "hypotheses": [],
                    "notes": (
                        "offline planner: no model available, "
                        "reviewing all changed files"
                    ),
                }
            )
        if self.role == "reviewer":
            if "scanner_findings" in names and "scanner_findings" not in called:
                return self._tool_use("scanner_findings", {})
            raw = self._strip_untrusted(called.get("scanner_findings", "[]"))
            try:
                items = json.loads(raw)
            except json.JSONDecodeError:
                items = []
            if (
                self.focus == "vulnerability"
                and "web_search" in names
                and "web_search" not in called
            ):
                hit = next(
                    (
                        it
                        for it in (items if isinstance(items, list) else [])
                        if isinstance(it, dict) and it.get("category") == self.focus
                    ),
                    None,
                )
                cwe = (hit or {}).get("cwe") or []
                query = (
                    f"{cwe[0]} {(hit or {}).get('rule', '')}".strip()
                    if cwe
                    else "CWE common weakness"
                )
                return self._tool_use("web_search", {"query": query, "max_results": 3})
            findings = []
            for it in items if isinstance(items, list) else []:
                if self.focus and it.get("category") != self.focus:
                    continue
                findings.append(
                    {
                        "title": f"{it.get('rule')}: {it.get('message')}",
                        "category": it.get("category", "bug"),
                        "severity": it.get("severity", "medium"),
                        "path": it.get("path"),
                        "line": it.get("line"),
                        "description": it.get("message", ""),
                        "rationale": (
                            f"Reported by static scanner {it.get('scanner')} "
                            "(offline mode)."
                        ),
                        "cwe": it.get("cwe", []),
                        "confidence": 0.6,
                        "suggested_fix": None,
                        "reproduction": None,
                        "scanner_rule": f"{it.get('scanner')}:{it.get('rule')}",
                    }
                )
            return self._final({"findings": findings})
        if self.role == "validator":
            if "run_tests" in names and "run_tests" not in called:
                return self._tool_use("run_tests", {"ref": "head"})
            finding = self._json_in(self._first_user_text(messages)) or {}
            backed = bool(finding.get("scanner_rule")) or str(
                finding.get("source", "")
            ).startswith("scanner")
            verdict = "confirmed" if backed else "unverified"
            return self._final(
                {
                    "verdict": verdict,
                    "notes": (
                        "Offline validator: finding is backed by a static scanner "
                        "rule; tests on head were executed and attached as evidence."
                    )
                    if backed
                    else (
                        "Offline validator cannot reason about this finding "
                        "without a model."
                    ),
                    "evidence": [
                        {
                            "kind": "reasoning",
                            "command": None,
                            "output": None,
                            "exit_code": None,
                            "ref": None,
                            "summary": (
                                "Static scanner rule matched on a changed line; "
                                "see the attached test run."
                                if backed
                                else "No model available to reason about the finding."
                            ),
                        }
                    ],
                    "fix": None,
                }
            )
        if self.role == "orchestrator":
            return self._orchestrator(messages, names, called)
        if self.role == "lead":
            return self._lead(messages, names, called)
        raise BackendError(f"unknown offline role {self.role}")

    _REVIEWERS = ("bugs", "vulnerabilities", "style")

    def _orchestrator(
        self, messages: list[dict[str, Any]], names: set[str], called: dict[str, str]
    ) -> BackendResponse:
        ran = [
            str((b.get("input") or {}).get("agent"))
            for m in messages
            if isinstance(m.get("content"), list)
            for b in m["content"]
            if isinstance(b, dict)
            and b.get("type") == "tool_use"
            and b.get("name") == "run_reviewer"
        ]
        if "run_reviewer" in names:
            todo = [a for a in self._REVIEWERS if a not in ran]
            if todo:
                return self._tool_use("run_reviewer", {"agent": todo[0]})
        if "list_candidates" in names and "list_candidates" not in called:
            return self._tool_use("list_candidates", {})
        try:
            pool = json.loads(
                self._strip_untrusted(called.get("list_candidates", "[]"))
            )
        except json.JSONDecodeError:
            pool = []
        ids = [str(c["id"]) for c in pool if isinstance(c, dict) and c.get("id")]
        if ids and "validate" in names and "validate" not in called:
            return self._tool_use("validate", {"ids": ids})
        return self._final(
            {
                "report": ids,
                "dropped": [],
                "notes": "offline orchestrator: no model available; ran every "
                "reviewer on the whole change and validated every candidate",
            }
        )

    _RANGE = re.compile(r"([\w./~^-]+)\.\.([\w./~^-]+)")
    _LAST_N = re.compile(r"(?i)last\s+(\d+)\s+commits?|最近\s*(\d+)\s*(?:个|次)?提交")
    _ALL = re.compile(
        r"(?i)\b(?:whole|entire|all files|full repo|everything)\b"
        r"|整个|全部|所有文件|全仓"
    )
    _VERIFY = re.compile(r"(?i)\b(?:verify|verifies|fixed|fixes|fix for)\b|验证|修复")
    _NEXT_ROUND = re.compile(
        r"(?i)\b(?:next|another|new|second|third)\s+(?:review\s+)?round\b"
        r"|\breview\s+round\s+\d|下一轮|新一轮"
    )

    def _lead(
        self, messages: list[dict[str, Any]], names: set[str], called: dict[str, str]
    ) -> BackendResponse:
        text = self._first_user_text(messages).split("User message:", 1)[-1].strip()
        if (
            self._NEXT_ROUND.search(text)
            and "start_next_round" in names
            and "start_next_round" not in called
        ):
            return self._tool_use("start_next_round", {})
        if (
            "start_review" in names
            and "start_review" not in called
            and "start_next_round" not in called
        ):
            args: dict[str, Any] = {"instructions": text}
            if m := self._RANGE.search(text):
                args["base"], args["head"] = m.group(1), m.group(2)
            elif m := self._LAST_N.search(text):
                n = m.group(1) or m.group(2)
                args["base"], args["head"] = f"HEAD~{n}", "HEAD"
            if self._ALL.search(text):
                args["scope"] = "all"
            if self._VERIFY.search(text):
                args["task"], args["claim"] = "verify_fix", text
            return self._tool_use("start_review", args)
        raw = called.get("start_review") or called.get("start_next_round") or "{}"
        try:
            s = json.loads(raw)
        except json.JSONDecodeError:  # the tool failed, e.g. no round left
            return BackendResponse(
                content=[{"type": "text", "text": f"Could not start: {raw[:400]}"}],
                stop_reason="end_turn",
                model=self.model,
            )
        if not isinstance(s, dict) or s.get("error"):
            why = s.get("error") if isinstance(s, dict) else raw[:400]
            return BackendResponse(
                content=[{"type": "text", "text": f"Could not start: {why}"}],
                stop_reason="end_turn",
                model=self.model,
            )
        c = s.get("counts") or {}
        head = (
            f"Review {s.get('run_id', '?')} finished on {s.get('range', '?')} "
            f"(scope {s.get('scope', '?')}): "
            f"{c.get('confirmed', 0)} confirmed, "
            f"{c.get('unverified', 0)} unverified, "
            f"{c.get('rejected', 0)} rejected."
        )
        lines = [head]
        if s.get("test_bench"):
            lines.append(f"Test bench: {s['test_bench']}.")
        for f in (s.get("findings") or [])[:5]:
            lines.append(
                f"- {f['id']} [{f['verdict']}] {f['severity']}: "
                f"{f['title']} ({f['location']})"
            )
        rnd = s.get("round") or {}
        if rnd:
            lines.append(
                f"Round {rnd.get('round')} of {rnd.get('max_rounds')}: "
                + (
                    "passed."
                    if rnd.get("passed")
                    else f"{len(rnd.get('pending') or [])} finding(s) pending"
                    + (
                        "; accept or reject them, then ask for the next round."
                        if rnd.get("can_continue")
                        else "."
                    )
                )
            )
        lines.append("")
        lines.append(
            "Offline mode: no model is available to reason further; "
            "open a finding for its evidence."
        )
        return BackendResponse(
            content=[{"type": "text", "text": "\n".join(lines)}],
            stop_reason="end_turn",
            model=self.model,
        )
