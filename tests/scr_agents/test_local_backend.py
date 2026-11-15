"""The local backend against a fake OpenAI-compatible server.

The fake speaks what vLLM / SGLang expose.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scr_agents.config import Settings
from scr_agents.llm.backend import BackendError
from scr_agents.llm.local import LocalBackend
from scr_agents.pipeline import make_backend_factory, resolve_mode


class FakeServer:
    """Scripted chat-completions responses; records every request body."""

    def __init__(self, replies=(), handler=None, models=None):
        """Bind a local HTTP server that answers from ``replies`` or ``handler``.

        ``models`` is the ``data`` list of ``GET /models``; by default one model.
        """
        self.replies = list(replies)
        self.models = models if models is not None else [{"id": "qwen3-local"}]
        self.handler = handler  # callable(body) -> reply dict; used when given
        self.requests = []
        self.headers = []  # the headers of every POST, in order
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                return None

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.endswith("/models"):
                    self._send(200, {"data": outer.models})
                else:
                    self._send(404, {"error": "nope"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                outer.headers.append(dict(self.headers.items()))
                if outer.handler is not None:
                    reply = outer.handler(body)
                elif outer.replies:
                    reply = outer.replies.pop(0)
                else:
                    self._send(500, {"error": "no scripted reply"})
                    return
                self._send(
                    200,
                    {
                        "id": "x",
                        "model": body["model"],
                        "choices": [
                            {
                                "index": 0,
                                "message": reply["message"],
                                "finish_reason": reply.get("finish_reason", "stop"),
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    },
                )

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        """Start serving in a background thread."""
        self.thread.start()
        return self

    def __exit__(self, *exc):
        """Stop the HTTP server."""
        self.httpd.shutdown()

    @property
    def base_url(self):
        """The ``/v1`` base URL the server listens on."""
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"


TOOL = {
    "name": "read_file",
    "description": "read",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
}


def test_tool_call_round_trip_and_message_conversion():
    """Tool calls, tool results and reasoning convert to and from the OpenAI format."""
    replies = [
        {
            "message": {
                "role": "assistant",
                "content": None,
                "reasoning_content": "let me look",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "a.py"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        },
        {
            "message": {
                "role": "assistant",
                "content": '{"findings": [], "summary": "clean"}',
            }
        },
    ]
    with FakeServer(replies) as srv:
        assert LocalBackend.reachable(srv.base_url)
        assert LocalBackend.list_models(srv.base_url) == ["qwen3-local"]
        b = LocalBackend(srv.base_url, "qwen3-local")
        r1 = b.complete(
            system="sys", messages=[{"role": "user", "content": "review"}], tools=[TOOL]
        )
        assert r1.stop_reason == "tool_use" and r1.tool_uses()[0]["input"] == {
            "path": "a.py"
        }
        assert r1.usage == {"input_tokens": 10, "output_tokens": 5}
        assert any(blk["type"] == "thinking" for blk in r1.content)
        sent = srv.requests[0]
        assert sent["messages"][0] == {"role": "system", "content": "sys"}
        assert (
            sent["tools"][0]["function"]["name"] == "read_file"
            and sent["tool_choice"] == "auto"
        )

        messages = [
            {"role": "user", "content": "review"},
            {"role": "assistant", "content": r1.content},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "content": "print(1)",
                    }
                ],
            },
        ]
        r2 = b.complete(
            system="sys",
            messages=messages,
            tools=[TOOL],
            output_schema={"type": "object"},
        )
        assert (
            r2.stop_reason == "end_turn" and json.loads(r2.text())["summary"] == "clean"
        )
        sent = srv.requests[1]
        assert (
            sent["messages"][2]["role"] == "assistant"
            and sent["messages"][2]["tool_calls"][0]["id"] == "call_1"
        )
        assert sent["messages"][3] == {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "print(1)",
        }
        assert len(srv.requests) == 2  # valid JSON: no repair round trip


def test_json_repair_uses_response_format():
    """Prose instead of JSON triggers one repair call with ``response_format``."""
    replies = [
        {
            "message": {
                "role": "assistant",
                "content": "Sure! The verdict is confirmed because...",
            }
        },
        {"message": {"role": "assistant", "content": '{"verdict": "confirmed"}'}},
    ]
    schema = {
        "type": "object",
        "properties": {"verdict": {"type": "string"}},
        "required": ["verdict"],
    }
    with FakeServer(replies) as srv:
        b = LocalBackend(srv.base_url, "qwen3-local")
        r = b.complete(
            system="s",
            messages=[{"role": "user", "content": "u"}],
            tools=[TOOL],
            output_schema=schema,
        )
        assert r.stop_reason == "end_turn" and json.loads(r.text()) == {
            "verdict": "confirmed"
        }
        repair = srv.requests[1]
        assert (
            repair["response_format"]["json_schema"]["schema"] == schema
            and "tools" not in repair
        )
        assert r.usage["input_tokens"] == 20


def test_invalid_tool_arguments_and_errors():
    """Bad tool-call JSON is kept as ``_invalid_json``; HTTP 500 raises an error."""
    replies = [
        {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{oops"},
                    }
                ],
            }
        }
    ]
    with FakeServer(replies) as srv:
        b = LocalBackend(srv.base_url, "qwen3-local", retries=0)
        r = b.complete(
            system="s", messages=[{"role": "user", "content": "u"}], tools=[TOOL]
        )
        assert r.tool_uses()[0]["input"] == {"_invalid_json": "{oops"}
        with pytest.raises(BackendError, match="HTTP 500"):
            b.complete(
                system="s", messages=[{"role": "user", "content": "u"}], tools=[]
            )
    assert not LocalBackend.reachable("http://127.0.0.1:9/v1")


def test_auto_mode_never_picks_the_online_api(monkeypatch):
    """``auto`` picks offline, never an online API, unless a local server answers."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-whatever")
    settings = Settings.load(
        overrides={
            "llm": {
                "mode": "auto",
                "local": {"base_url": "http://127.0.0.1:9/v1"},
                "openai": {"model": "some-hosted-model"},
            }
        }
    )
    assert resolve_mode(settings) == "offline"
    with FakeServer([]) as srv:
        settings = Settings.load(
            overrides={"llm": {"mode": "auto", "local": {"base_url": srv.base_url}}}
        )
        assert resolve_mode(settings) == "local"


def test_a_server_answering_html_is_not_reachable() -> None:
    """Another web app on the model port makes ``auto`` fall back, not crash."""

    class Html(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"<html>not a model server</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Html)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
        assert LocalBackend.reachable(url) is False
        settings = Settings.load(
            overrides={"llm": {"mode": "auto", "local": {"base_url": url}}}
        )
        assert resolve_mode(settings) == "offline"
    finally:
        httpd.shutdown()


def test_served_models_reads_max_model_len() -> None:
    """``max_model_len`` is read per model; a missing or invalid one is ``None``."""
    models = [
        {"id": "gemma", "max_model_len": 65536},
        {"id": "llama", "max_model_len": 32768},
        {"id": "plain"},
        {"id": "odd", "max_model_len": "big"},
        {"id": "flag", "max_model_len": True},
    ]
    with FakeServer(models=models) as srv:
        assert LocalBackend.served_models(srv.base_url) == {
            "gemma": 65536,
            "llama": 32768,
            "plain": None,
            "odd": None,
            "flag": None,
        }
        assert LocalBackend.list_models(srv.base_url) == [m["id"] for m in models]
        assert LocalBackend.served_context_window(srv.base_url, "llama") == 32768
        assert LocalBackend.served_context_window(srv.base_url, "missing") is None
    # the server is gone now: unreachable means unknown, not an error
    gone = LocalBackend.served_context_window(srv.base_url, "llama", timeout=0.5)
    assert gone is None


def test_each_backend_gets_its_servers_context_window() -> None:
    """Reviewer and validator use their own server's length, else the config value."""
    reviewer = FakeServer(models=[{"id": "gemma", "max_model_len": 65536}])
    validator = FakeServer(models=[{"id": "llama"}])  # no max_model_len reported
    with reviewer, validator:
        settings = Settings.load(
            overrides={
                "llm": {
                    "mode": "local",
                    "local": {
                        "base_url": reviewer.base_url,
                        "model": "gemma",
                        "validator_base_url": validator.base_url,
                        "validator_model": "llama",
                        "context_window": 12345,
                    },
                }
            }
        )
        factory, mode = make_backend_factory(settings)
        assert mode == "local"
        assert factory("bugs").context_window == 65536
        assert factory("validator").context_window == 12345


def test_llm_check_also_checks_a_separate_validator_server(monkeypatch, tmp_path):
    """``llm check`` tests the validator's server unless one target is named."""
    import argparse

    from scr_agents import cli

    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "llm:\n  local:\n    base_url: http://r:1/v1\n    model: rev\n"
        "    validator_base_url: http://v:2/v1\n    validator_model: val\n"
    )
    calls: list[tuple[str, str, str]] = []

    def fake(settings, role, base_url, model):
        calls.append((role, base_url, model))
        return role == "reviewer"

    monkeypatch.setattr(cli, "_check_llm_server", fake)
    args = argparse.Namespace(config=str(cfg), base_url=None, model=None)
    assert cli.cmd_llm(args) == 1  # the validator's check failed
    assert calls == [
        ("reviewer", "http://r:1/v1", "rev"),
        ("validator", "http://v:2/v1", "val"),
    ]
    calls.clear()
    args = argparse.Namespace(config=str(cfg), base_url="http://x:3/v1", model=None)
    assert cli.cmd_llm(args) == 0
    assert calls == [("reviewer", "http://x:3/v1", "rev")]


def _openai_settings(base_url: str, **openai: object) -> Settings:
    return Settings.load(
        overrides={
            "llm": {"mode": "openai", "openai": {"base_url": base_url, **openai}}
        }
    )


def test_openai_mode_sends_the_environment_key_and_the_hosted_request_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mode: openai`` reads the key from the environment and adapts the request."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    with FakeServer([{"message": {"role": "assistant", "content": "ok"}}]) as srv:
        settings = _openai_settings(
            srv.base_url, model="hosted-rev", validator_model="hosted-val"
        )
        factory, mode = make_backend_factory(settings)
        assert mode == "openai"
        reviewer, validator = factory("bugs"), factory("validator")
        assert (reviewer.model, validator.model) == ("hosted-rev", "hosted-val")
        assert reviewer.name == "openai" and reviewer.context_window == 128000
        r = reviewer.complete(
            system="s", messages=[{"role": "user", "content": "hi"}], tools=[TOOL]
        )
        assert r.text() == "ok"
        (body,) = srv.requests
        assert body["model"] == "hosted-rev" and body["max_completion_tokens"] == 8000
        assert "max_tokens" not in body and "temperature" not in body
        assert srv.headers[0]["Authorization"] == "Bearer sk-from-env"


def test_openai_mode_can_send_a_temperature_and_the_older_token_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Other hosted OpenAI-compatible APIs keep ``max_tokens`` and a temperature."""
    monkeypatch.setenv("OTHER_KEY", "k")
    with FakeServer([{"message": {"role": "assistant", "content": "ok"}}]) as srv:
        settings = _openai_settings(
            srv.base_url,
            model="m",
            api_key_env="OTHER_KEY",
            token_param="max_tokens",
            temperature=0.1,
        )
        factory, _ = make_backend_factory(settings)
        factory("bugs").complete(
            system="s", messages=[{"role": "user", "content": "hi"}], tools=[]
        )
        (body,) = srv.requests
        assert body["max_tokens"] == 8000 and body["temperature"] == 0.1
        assert "max_completion_tokens" not in body
        assert srv.headers[0]["Authorization"] == "Bearer k"


def test_openai_mode_needs_a_model_and_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing model or key stops the run with a message that names the setting."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match=r"llm\.openai\.model is empty"):
        make_backend_factory(_openai_settings("http://127.0.0.1:9/v1"))
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not set"):
        make_backend_factory(_openai_settings("http://127.0.0.1:9/v1", model="m"))


def test_llm_check_checks_the_hosted_api_in_openai_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``llm check`` tests the hosted reviewer and a different validator model."""
    import argparse

    from scr_agents import cli

    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "llm:\n  mode: openai\n  openai:\n    base_url: http://h:1/v1\n"
        "    model: rev\n    validator_model: val\n"
    )
    calls: list[tuple[str, str, str, bool]] = []

    def fake(settings, role, base_url, model, hosted=False):
        calls.append((role, base_url, model, hosted))
        return True

    monkeypatch.setattr(cli, "_check_llm_server", fake)
    args = argparse.Namespace(config=str(cfg), base_url=None, model=None)
    assert cli.cmd_llm(args) == 0
    assert calls == [
        ("reviewer", "http://h:1/v1", "rev", True),
        ("validator", "http://h:1/v1", "val", True),
    ]
    assert "--mode openai" in capsys.readouterr().out


def test_llm_check_reports_a_missing_key_without_calling_the_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without the key the hosted check stops before any request."""
    from scr_agents import cli

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with FakeServer([]) as srv:
        settings = _openai_settings(srv.base_url, model="m")
        assert (
            cli._check_llm_server(settings, "reviewer", srv.base_url, "m", True)
            is False
        )
        assert srv.requests == []
    assert "NO KEY (OPENAI_API_KEY is not set)" in capsys.readouterr().out


def test_anthropic_mode_says_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the SDK or the key the run stops with the fix, before any agent."""
    import importlib.util

    from scr_agents import pipeline

    settings = Settings.load(overrides={"llm": {"mode": "anthropic"}})
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(RuntimeError, match="uv sync --extra anthropic"):
        make_backend_factory(settings)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(pipeline, "has_anthropic_credentials", lambda: False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        make_backend_factory(settings)
