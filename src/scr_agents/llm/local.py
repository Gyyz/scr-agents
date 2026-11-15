"""OpenAI-compatible backend: any chat-completions server.

That covers vLLM and SGLang (also llama.cpp server, Ollama, LM Studio), which keep
everything on your own machines, and, only with ``llm.mode: openai``, a hosted API
such as OpenAI's.

The agents speak the Anthropic-style block dialect used by the tool loop; this module
converts to and from the OpenAI chat format: system/user/assistant/tool messages,
`tools` as functions, `tool_calls` with JSON-string arguments, `response_format` for
the final structured answer.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from scr_agents.llm.backend import BackendError, BackendResponse

_FINISH = {
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
    "tool_calls": "tool_use",
}


class LocalBackend:
    """Calls an OpenAI-compatible chat-completions server, such as vLLM or SGLang.

    Converts the tool loop's block dialect to the chat format and back, retries
    requests that fail to connect or get a server error, and can ask once more for a
    JSON answer when a final answer that needs a schema is not one.
    """

    name = "local"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "EMPTY",
        temperature: float | None = 0.2,
        max_tokens: int = 8000,
        timeout_s: float = 600.0,
        json_repair: bool = True,
        extra_body: dict[str, Any] | None = None,
        retries: int = 2,
        context_window: int | None = None,
        token_param: str = "max_tokens",
        name: str = "local",
    ):
        """Configure the server and the request defaults; nothing is sent yet.

        Args:
            base_url: The server's API root, such as ``http://host:8010/v1``.
            model: The served model name sent with every request.
            api_key: Bearer token; servers without authentication accept ``EMPTY``.
            temperature: Sampling temperature (the JSON repair request uses 0).
                ``None`` leaves it out of every request, for hosted models that
                accept only their default.
            max_tokens: Cap on the tokens generated per request, whatever the caller
                asks for.
            timeout_s: Timeout of each HTTP request, in seconds.
            json_repair: Ask once more, with constrained decoding, when a final answer
                that needs a schema is not a JSON object.
            extra_body: Fields merged into every request body, for server-specific
                options.
            retries: Extra attempts after a connection error or a 5xx response.
            context_window: The server's context length in tokens, if known.
            token_param: The request field that caps the generated tokens:
                ``max_tokens``, or ``max_completion_tokens`` for OpenAI's API.
            name: Recorded with every model call: ``local`` or ``openai``.
        """
        self.base_url = base_url.rstrip("/")
        # the server's --max-model-len; the tool loop budgets against it
        self.context_window = context_window
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.json_repair = json_repair
        self.extra_body = dict(extra_body or {})
        self.retries = retries
        self.token_param = token_param
        self.name = name

    def _sampling(self, max_tokens: int, temperature: float | None) -> dict[str, Any]:
        """Return the output cap and, unless left out, the temperature of a request."""
        fields: dict[str, Any] = {self.token_param: min(max_tokens, self.max_tokens)}
        if self.temperature is not None and temperature is not None:
            fields["temperature"] = temperature
        return fields

    # ------------------------------------------------------------------ http
    @staticmethod
    def _request(
        method: str,
        url: str,
        api_key: str,
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise BackendError(f"{url} returned a non-JSON body: {body[:200]!r}") from e
        if not isinstance(data, dict):
            raise BackendError(
                f"{url} returned {type(data).__name__}, expected an object"
            )
        return data

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        exchanges: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """POST with retries.

        Every attempt is appended to `exchanges` (URL, body sent, status, body received,
        duration); headers are not recorded, so the API key never reaches a trace.
        """
        url = f"{self.base_url}{path}"
        log = exchanges if exchanges is not None else []
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            t0 = time.monotonic()
            rec: dict[str, Any] = {
                "api": "openai-compatible",
                "url": url,
                "attempt": attempt + 1,
                "request": payload,
            }
            log.append(rec)
            try:
                data = self._request("POST", url, self.api_key, payload, self.timeout_s)
                rec.update(
                    status=200,
                    response=data,
                    duration_s=round(time.monotonic() - t0, 3),
                )
                return data
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")[:2000]
                rec.update(
                    status=e.code,
                    error=body,
                    duration_s=round(time.monotonic() - t0, 3),
                )
                if e.code < 500 or attempt == self.retries:
                    raise BackendError(
                        f"{url} returned HTTP {e.code}: {body}", log
                    ) from e
                last = e
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                http.client.HTTPException,
            ) as e:
                rec.update(
                    status=None,
                    error=f"{type(e).__name__}: {e}",
                    duration_s=round(time.monotonic() - t0, 3),
                )
                last = e
                if attempt == self.retries:
                    raise BackendError(
                        f"cannot reach local LLM at {url}: {e}", log
                    ) from e
            except BackendError as e:  # non-JSON or non-object body
                rec.update(
                    status=200, error=str(e), duration_s=round(time.monotonic() - t0, 3)
                )
                e.exchanges = log
                raise
            time.sleep(1.5 * (attempt + 1))
        raise BackendError(f"local LLM request failed: {last}", log)

    @classmethod
    def served_models(
        cls, base_url: str, api_key: str = "EMPTY", timeout: float = 3.0
    ) -> dict[str, int | None]:
        """Return each served model's ID with its context length (``GET /models``).

        vLLM reports the context length as ``max_model_len``; a model without a
        positive integer there maps to ``None``.

        Raises:
            OSError: The server cannot be reached or answers with an error status.
            BackendError: The reply is not a JSON object.
        """
        data = cls._request(
            "GET", f"{base_url.rstrip('/')}/models", api_key, None, timeout
        )
        served: dict[str, int | None] = {}
        for m in data.get("data", []):
            if not isinstance(m, dict):
                continue
            n = m.get("max_model_len")
            ok = isinstance(n, int) and not isinstance(n, bool) and n > 0
            served[m.get("id", "")] = n if ok else None
        return served

    @classmethod
    def list_models(
        cls, base_url: str, api_key: str = "EMPTY", timeout: float = 3.0
    ) -> list[str]:
        """Return the IDs of the models the server serves (``GET /models``).

        Raises:
            OSError: The server cannot be reached or answers with an error status.
            BackendError: The reply is not a JSON object.
        """
        return list(cls.served_models(base_url, api_key, timeout))

    @classmethod
    def served_context_window(
        cls, base_url: str, model: str, api_key: str = "EMPTY", timeout: float = 3.0
    ) -> int | None:
        """Return the context length the server reports for ``model``, or ``None``.

        ``None`` when the server cannot be reached, does not list the model or does
        not report a ``max_model_len`` for it.
        """
        try:
            return cls.served_models(base_url, api_key, timeout).get(model)
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            ValueError,
            OSError,
            BackendError,
        ):
            return None

    @classmethod
    def reachable(
        cls, base_url: str, api_key: str = "EMPTY", timeout: float = 1.5
    ) -> bool:
        """Tell whether ``GET /models`` succeeds within ``timeout`` seconds.

        A server that answers with something other than a JSON object (another web
        app on that port, a proxy's error page) counts as not reachable.
        """
        try:
            cls.list_models(base_url, api_key, timeout)
            return True
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            ValueError,
            OSError,
            BackendError,
        ):
            return False

    # ------------------------------------------------------------------ conversion
    @staticmethod
    def to_openai_messages(
        system: str, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Convert the system prompt and the block-dialect messages to chat messages.

        ``tool_use`` blocks become ``tool_calls``, each ``tool_result`` a ``tool``
        message (prefixed with ``ERROR:`` when the tool failed), and the other text of
        such a user turn one user message after them.
        """
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            role, content = m["role"], m["content"]
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue
            if role == "assistant":
                text = "".join(
                    b.get("text", "") for b in content if b.get("type") == "text"
                )
                calls = [
                    {
                        "id": b["id"],
                        "type": "function",
                        "function": {
                            "name": b["name"],
                            "arguments": json.dumps(b.get("input") or {}),
                        },
                    }
                    for b in content
                    if b.get("type") == "tool_use"
                ]
                msg: dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
                continue
            # user turn made of tool results (and possibly text)
            texts = []
            for b in content:
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": b["tool_use_id"],
                            "content": ("ERROR: " if b.get("is_error") else "")
                            + str(c or ""),
                        }
                    )
                elif b.get("type") == "text":
                    texts.append(b["text"])
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
        return out

    @staticmethod
    def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert tool definitions (``ToolSpec.to_api`` shape) to chat functions."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object"},
                },
            }
            for t in tools
        ]

    @staticmethod
    def _parse_choice(data: dict[str, Any]) -> tuple[list[dict[str, Any]], str, str]:
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise BackendError(
                f"local LLM returned no usable choices: {json.dumps(data)[:500]}"
            )
        choice = choices[0]
        msg = choice.get("message") or {}
        if not isinstance(msg, dict):
            raise BackendError(
                f"local LLM returned a malformed message: {json.dumps(choice)[:500]}"
            )
        blocks: list[dict[str, Any]] = []
        reasoning = msg.get("reasoning_content") or msg.get("reasoning")
        if reasoning:
            blocks.append({"type": "thinking", "thinking": str(reasoning)})
        text = msg.get("content")
        if isinstance(text, list):  # some servers return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        if text:
            blocks.append({"type": "text", "text": text})
        calls = msg.get("tool_calls") or []
        for call in calls:
            fn = call.get("function") or {}
            raw = fn.get("arguments")
            if isinstance(raw, dict):
                args = raw
            else:
                try:
                    args = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    args = {"_invalid_json": raw}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "name": fn.get("name", ""),
                    "input": args,
                }
            )
        finish = choice.get("finish_reason") or "stop"
        stop = "tool_use" if calls else _FINISH.get(finish, "end_turn")
        return blocks, stop, str(text or "")

    @staticmethod
    def _usage(data: dict[str, Any]) -> dict[str, int]:
        u = data.get("usage") or {}
        out = {
            "input_tokens": int(u.get("prompt_tokens") or 0),
            "output_tokens": int(u.get("completion_tokens") or 0),
        }
        # prompt tokens served from the prefix (KV) cache; only reported by vLLM with
        # --enable-prompt-tokens-details (SGLang: --enable-cache-report)
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
        if cached is not None:
            out["cached_input_tokens"] = int(cached)
        return out

    # ------------------------------------------------------------------ main call
    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        output_schema: dict[str, Any] | None = None,
        max_tokens: int = 16000,
    ) -> BackendResponse:
        """Run one model turn on the server's chat-completions endpoint.

        Takes the same arguments as ``LLMBackend.complete``. Every HTTP exchange is
        attached to the response, or to the error raised.

        Raises:
            BackendError: The server could not be reached, answered with an error or
                sent a malformed completion.
        """
        exchanges: list[dict[str, Any]] = []
        try:
            resp = self._complete(
                exchanges, system, messages, tools, output_schema, max_tokens
            )
        except BackendError as e:
            e.exchanges = exchanges
            raise
        resp.exchanges = exchanges
        return resp

    def _complete(
        self,
        ex: list[dict[str, Any]],
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        output_schema: dict[str, Any] | None,
        max_tokens: int,
    ) -> BackendResponse:
        oai_messages = self.to_openai_messages(system, messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": oai_messages,
            **self._sampling(max_tokens, self.temperature),
        }
        if tools:
            payload["tools"] = self.to_openai_tools(tools)
            payload["tool_choice"] = "auto"
        payload.update(self.extra_body)
        data = self._post("/chat/completions", payload, ex)
        try:
            blocks, stop, text = self._parse_choice(data)
            usage = self._usage(data)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise BackendError(
                f"malformed chat completion from {self.base_url}: "
                f"{type(e).__name__}: {e}"
            ) from e

        if (
            output_schema is not None
            and stop == "end_turn"
            and self.json_repair
            and not _is_json_object(text)
        ):
            # Final answer was prose or malformed: ask once more with constrained
            # decoding and no tools.
            repair_messages = [
                *oai_messages,
                {"role": "assistant", "content": text or "(empty)"},
                {
                    "role": "user",
                    "content": (
                        "Return exactly the same answer as one JSON object that "
                        "matches the required schema. No prose, no code fences."
                    ),
                },
            ]
            repair: dict[str, Any] = {
                "model": self.model,
                "messages": repair_messages,
                **self._sampling(max_tokens, 0),
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "output", "schema": output_schema},
                },
            }
            repair.update(self.extra_body)
            data2 = self._post("/chat/completions", repair, ex)
            ex[-1]["purpose"] = "json repair"
            blocks2, _, text2 = self._parse_choice(data2)
            u2 = self._usage(data2)
            usage = {k: usage.get(k, 0) + u2.get(k, 0) for k in set(usage) | set(u2)}
            blocks = [b for b in blocks if b.get("type") == "thinking"] + [
                b for b in blocks2 if b.get("type") != "thinking"
            ]
            if not any(b.get("type") == "text" for b in blocks):
                blocks.append({"type": "text", "text": text2})
        return BackendResponse(
            content=blocks,
            stop_reason=stop,
            usage=usage,
            model=str(data.get("model") or self.model),
        )


def _is_json_object(text: str) -> bool:
    from scr_agents.llm.loop import parse_json_object

    return parse_json_object(text or "") is not None
