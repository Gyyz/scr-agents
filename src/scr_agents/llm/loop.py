"""The agentic tool-use loop shared by every WS1 agent.

Owns: dispatching tool calls, routing untrusted results through the safety filter,
logging to the activity log, capping turns, parsing the final structured output, and
running tools marked `parallel_safe=False` one at a time across every loop that shares
the same `serial_lock`.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, cast

from scr_agents.llm.backend import (
    TOOL_CALLER,
    BackendError,
    BackendResponse,
    LLMBackend,
    ToolSpec,
)
from scr_agents.ws4_guardrails.safety_filter import SafetyFilter
from scr_agents.ws5_knowledge.activity_log import ActivityLog

MAX_TOOL_RESULT_CHARS = 30000
PREVIEW_CHARS = 1500
# compact old tool results once prompt + answer would exceed this share of the window
CONTEXT_HEADROOM = 0.9
_CONTEXT_ERROR = re.compile(
    r"(?i)context.length|maximum context|max_model_len|too many tokens|"
    r"context_length_exceeded|reduce the length|exceeds? the (?:model's )?limit"
)
FINAL_TURN_NOTE = (
    "No tool turns remain. Answer now with the required JSON object only, "
    "based on what you have already read."
)


def _short(obj: Any, n: int = 600) -> str:
    s = json.dumps(obj, default=str)
    return s if len(s) <= n else s[:n] + "..."


@dataclass
class ToolCall:
    """One tool call the loop ran: its input, the output the model saw, and timing."""

    name: str
    input: dict[str, Any]
    output: str
    is_error: bool = False
    duration_s: float = 0.0


@dataclass
class LoopResult:
    """What one agent run produced: the final answer, usage and every tool call.

    ``data`` is the parsed JSON answer when an output schema was given. ``error`` is
    set when the run ended without a usable answer.
    """

    text: str = ""
    data: dict[str, Any] | None = None
    stop_reason: str = ""
    turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)
    error: str | None = None
    model: str = ""

    @property
    def ok(self) -> bool:
        """Whether the run ended without an error."""
        return self.error is None


_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```")


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Extract the JSON object from a model's answer, or return None.

    Tries the whole text, then each fenced code block, then the span from the first
    ``{`` to the last ``}``. Only an object counts, not another JSON value.
    """
    text = text.strip()
    for candidate in [text, *(m.group(1).strip() for m in _FENCE.finditer(text))]:
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


class ToolLoop:
    """Runs one agent: calls the model and the tools it asks for until it answers.

    Untrusted tool results go through the safety filter, every step is logged to the
    activity log, and old tool results are compacted when the context window fills.
    """

    def __init__(
        self,
        backend: LLMBackend,
        tools: list[ToolSpec],
        safety: SafetyFilter,
        log: ActivityLog,
        agent: str,
        max_turns: int = 24,
        max_tokens: int = 16000,
        serial_lock: threading.Lock | None = None,
    ):
        """Set up the loop for one agent.

        Args:
            backend: The model to call.
            tools: The tools offered to the model; their names must be unique.
            safety: Filter that wraps the results of untrusted tools.
            log: Activity log that records every model and tool call.
            agent: The agent's name in the log and the trace files.
            max_turns: Model turns allowed before the run is stopped.
            max_tokens: Upper bound on the tokens generated per model call.
            serial_lock: Lock shared by the loops whose tools must not run at the same
                time; tools that are not ``parallel_safe`` take it.
        """
        self.backend = backend
        # taken around tools that are not parallel_safe (shared checkouts)
        self.serial_lock = serial_lock
        self.tools = {t.name: t for t in tools}
        self.safety = safety
        self.log = log
        self.agent = agent
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self.context_window: int | None = getattr(
            backend, "context_window", None
        )  # tokens, if the backend knows

    def _compact(self, messages: list[dict[str, Any]], keep: int) -> int:
        """Replace the content of old tool results with one-line stubs.

        Keeps the `keep` newest result turns intact. Returns the number of characters
        freed.
        """
        names: dict[str, str] = {}
        result_turns = []
        for m in messages:
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    names[b.get("id", "")] = b.get("name", "?")
            if m.get("role") == "user" and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                result_turns.append(content)
        freed = 0
        for turn_no, content in enumerate(
            result_turns[: max(0, len(result_turns) - keep)], 1
        ):
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                text = b.get("content")
                if not isinstance(text, str) or text.startswith("[result elided"):
                    continue
                stub = (
                    f"[result elided after turn {turn_no} to free context: "
                    f"tool {names.get(b.get('tool_use_id', ''), '?')}, "
                    f"{len(text)} chars; call it again if you need it]"
                )
                freed += len(text) - len(stub)
                b["content"] = stub
        return freed

    def _exec(self, call: dict[str, Any]) -> ToolCall:
        name, inp = call["name"], call.get("input") or {}
        spec = self.tools.get(name)
        origin = getattr(spec, "origin", "") if spec is not None else ""
        extra = (
            {"origin": origin, "remote_name": spec.remote_name}
            if spec is not None and origin
            else {}
        )
        self.log.log(
            "tool_start",
            self.agent,
            tool=name,
            input=inp,
            call_id=call.get("id"),
            **extra,
        )
        t0 = time.monotonic()
        if spec is None:
            return ToolCall(name, inp, f"unknown tool: {name}", True, 0.0)
        if isinstance(inp, dict) and "_invalid_json" in inp:
            return ToolCall(
                name,
                inp,
                f"tool arguments were not valid JSON: {inp['_invalid_json']!r}",
                True,
                0.0,
            )
        token = TOOL_CALLER.set({"agent": self.agent, "call_id": call.get("id")})
        try:
            if spec.parallel_safe or self.serial_lock is None:
                out = spec.handler(inp)
            else:
                with self.serial_lock:
                    out = spec.handler(inp)
            if not isinstance(out, str):
                out = json.dumps(out, default=str, indent=None)
            is_error = False
        except Exception as e:
            out, is_error = f"{type(e).__name__}: {e}", True
        finally:
            TOOL_CALLER.reset(token)
        if len(out) > MAX_TOOL_RESULT_CHARS:
            out = (
                out[:MAX_TOOL_RESULT_CHARS]
                + f"\n...[tool output truncated at {MAX_TOOL_RESULT_CHARS} chars]"
            )
        if (
            spec.untrusted
        ):  # errors too: an exception message can carry repository content
            out = self.safety.wrap(out, f"tool:{name}")
        return ToolCall(name, inp, out, is_error, time.monotonic() - t0)

    def _write_trace(self, name: str, data: dict[str, Any]) -> str | None:
        writer = getattr(self.log, "write_trace", None)
        if writer is None:
            return None
        try:
            return cast("str | None", writer(name, data))
        except Exception:
            return None

    def _trace_llm(
        self,
        turn: int,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        output_schema: dict[str, Any] | None,
        resp: BackendResponse | None,
        t_call: float,
        error: str | None = None,
        exchanges: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Full record of one model call, written as a trace file.

        It holds the request as the loop built it, the response, and the raw API traffic
        the backend reported (exact HTTP bodies for the OpenAI-compatible backend).
        """
        data: dict[str, Any] = {
            "kind": "llm",
            "agent": self.agent,
            "turn": turn,
            "backend": getattr(self.backend, "name", ""),
            "model": getattr(self.backend, "model", ""),
            "duration_s": round(time.monotonic() - t_call, 3),
            "request": {
                "system": system,
                "messages": messages,
                "tools": tools,
                "output_schema": output_schema,
                "max_tokens": self.max_tokens,
            },
        }
        if resp is not None:
            data["response"] = {
                "model": resp.model,
                "stop_reason": resp.stop_reason,
                "usage": resp.usage,
                "content": resp.content,
                "stop_details": resp.stop_details,
            }
            exchanges = resp.exchanges
        if error is not None:
            data["error"] = error
        data["exchanges"] = exchanges or []
        return self._write_trace(f"{self.agent}-turn{turn}", data)

    def run(
        self, system: str, user: str, output_schema: dict[str, Any] | None = None
    ) -> LoopResult:
        """Run the agent until it answers, fails or runs out of turns.

        Args:
            system: The system prompt.
            user: The first user message: the agent's task.
            output_schema: JSON schema of the final answer. With it, the answer is
                parsed into ``LoopResult.data``, and the last turn offers no tools so
                the model has to answer.

        Returns:
            The result. Failures (a backend error, a refusal, a truncated answer, too
            many turns, an answer that is not a JSON object) are reported in its
            ``error`` instead of being raised.
        """
        api_tools = [t.to_api() for t in self.tools.values()]
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        result = LoopResult(model=getattr(self.backend, "model", ""))
        usage: dict[str, int] = {}
        for turn in range(1, self.max_turns + 1):
            result.turns = turn
            final_turn = (
                turn == self.max_turns and output_schema is not None and api_tools
            )
            if final_turn:  # last chance: no tools, just the answer
                last = messages[-1]
                if isinstance(last.get("content"), list):
                    last["content"].append({"type": "text", "text": FINAL_TURN_NOTE})
                else:
                    messages.append({"role": "user", "content": FINAL_TURN_NOTE})
            self.log.log(
                "llm_start",
                self.agent,
                turn=turn,
                model=getattr(self.backend, "model", ""),
            )
            retried = False
            while True:
                call_tools = [] if final_turn else api_tools
                t_call = time.monotonic()
                try:
                    resp: BackendResponse = self.backend.complete(
                        system=system,
                        messages=messages,
                        tools=call_tools,
                        output_schema=output_schema,
                        max_tokens=self.max_tokens,
                    )
                    break
                except BackendError as e:
                    if not retried and _CONTEXT_ERROR.search(str(e)):
                        freed = self._compact(messages, keep=0)
                        if freed:
                            retried = True
                            self.log.log(
                                "context_compacted",
                                self.agent,
                                turn=turn,
                                freed_chars=freed,
                                reason="context length error",
                            )
                            continue
                    result.error = str(e)
                    trace = self._trace_llm(
                        turn,
                        system,
                        messages,
                        call_tools,
                        output_schema,
                        None,
                        t_call,
                        error=str(e),
                        exchanges=e.exchanges,
                    )
                    self.log.log(
                        "llm_error", self.agent, error=str(e), turn=turn, trace=trace
                    )
                    result.usage = usage
                    return result
                except Exception as e:
                    result.error = f"{type(e).__name__}: {e}"
                    trace = self._trace_llm(
                        turn,
                        system,
                        messages,
                        call_tools,
                        output_schema,
                        None,
                        t_call,
                        error=result.error,
                    )
                    self.log.log(
                        "llm_error",
                        self.agent,
                        error=result.error,
                        turn=turn,
                        trace=trace,
                    )
                    result.usage = usage
                    return result
            for k, v in resp.usage.items():
                usage[k] = usage.get(k, 0) + int(v or 0)
            # the request is serialized now, before messages grow or get compacted
            trace = self._trace_llm(
                turn, system, messages, call_tools, output_schema, resp, t_call
            )
            self.log.log(
                "llm_call",
                self.agent,
                model=resp.model,
                turn=turn,
                stop_reason=resp.stop_reason,
                usage=resp.usage,
                tool_uses=[t["name"] for t in resp.tool_uses()],
                tool_inputs=[
                    {"name": t["name"], "input": _short(t.get("input"))}
                    for t in resp.tool_uses()
                ],
                text_preview=resp.text()[:PREVIEW_CHARS],
                duration_s=round(time.monotonic() - t_call, 3),
                trace=trace,
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": resp.raw_content
                    if resp.raw_content is not None
                    else resp.content,
                }
            )
            result.stop_reason = resp.stop_reason

            if resp.stop_reason == "tool_use":
                results = []
                for call in resp.tool_uses():
                    tc = self._exec(call)
                    result.tool_calls.append(tc)
                    spec = self.tools.get(tc.name)
                    origin = getattr(spec, "origin", "") if spec is not None else ""
                    extra = (
                        {"origin": origin, "remote_name": spec.remote_name}
                        if spec is not None and origin
                        else {}
                    )
                    trace = self._write_trace(
                        f"{self.agent}-{tc.name}",
                        {
                            "kind": "tool",
                            "agent": self.agent,
                            "turn": turn,
                            "tool": tc.name,
                            "call_id": call.get("id"),
                            "input": tc.input,
                            "output": tc.output,
                            "is_error": tc.is_error,
                            "duration_s": round(tc.duration_s, 3),
                            **extra,
                        },
                    )
                    self.log.log(
                        "tool_call",
                        self.agent,
                        tool=tc.name,
                        input=tc.input,
                        call_id=call.get("id"),
                        **extra,
                        is_error=tc.is_error,
                        duration_s=round(tc.duration_s, 3),
                        output_chars=len(tc.output),
                        output_preview=tc.output[:PREVIEW_CHARS],
                        trace=trace,
                    )
                    block: dict[str, Any] = {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "content": tc.output,
                    }
                    if tc.is_error:
                        block["is_error"] = True
                    results.append(block)
                messages.append(
                    {"role": "user", "content": results}
                )  # all results in one turn
                prompt_tokens = int(resp.usage.get("input_tokens") or 0)
                if (
                    self.context_window
                    and prompt_tokens + self.max_tokens
                    > CONTEXT_HEADROOM * self.context_window
                ):
                    freed = self._compact(messages, keep=1)
                    if freed:
                        self.log.log(
                            "context_compacted",
                            self.agent,
                            turn=turn,
                            freed_chars=freed,
                            reason=(
                                f"{prompt_tokens} prompt tokens of "
                                f"{self.context_window}"
                            ),
                        )
                continue
            if resp.stop_reason == "pause_turn":
                continue
            if resp.stop_reason == "refusal":
                result.error = f"model refused: {resp.stop_details or {}}"
                break
            if resp.stop_reason == "max_tokens":
                result.error = "response truncated at max_tokens"
                break
            # end_turn / stop_sequence
            result.text = resp.text()
            if output_schema is not None:
                result.data = parse_json_object(result.text)
                if result.data is None:
                    result.error = "final response was not a JSON object"
            break
        else:
            result.error = f"agent exceeded max_turns={self.max_turns}"
        result.usage = usage
        return result
