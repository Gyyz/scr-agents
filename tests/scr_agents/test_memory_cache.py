"""The data behind the console's Memory panel.

What the agents remembered (memory_used) and how much of their prompts the model
server's KV (prefix) cache served (per call from usage, per server from /metrics).
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from starlette.testclient import TestClient

from scr_agents.config import Settings
from scr_agents.llm.local import LocalBackend
from scr_agents.llm.server_metrics import CacheMonitor, metrics_url, parse_metrics
from scr_agents.web.server import create_app

VLLM_V1 = (
    """# HELP vllm:prefix_cache_queries_total Prefix cache queries, """
    """in terms of number of queried tokens.
# TYPE vllm:prefix_cache_queries_total counter
vllm:prefix_cache_queries_total{engine="0",model_name="m"} 1000.0
vllm:prefix_cache_queries_total{engine="1",model_name="m"} 500.0
vllm:prefix_cache_hits_total{engine="0",model_name="m"} 600.0
vllm:prefix_cache_hits_total{engine="1",model_name="m"} 300.0
vllm:prefix_cache_hits_total{engine="0",model_name="other"} 99999.0
vllm:kv_cache_usage_perc{engine="0",model_name="m"} 0.25
vllm:kv_cache_usage_perc{engine="1",model_name="m"} 0.75
"""
)


def test_parse_metrics_vllm_v1_older_vllm_and_sglang():
    """Cache metrics parse from vLLM v1, older vLLM and SGLang ``/metrics`` text."""
    got = parse_metrics(VLLM_V1, model="m")
    assert got == {"queries": 1500.0, "hits": 900.0, "usage": 0.5}, (
        "summed over engines, other models skipped"
    )
    assert parse_metrics(
        'vllm:gpu_prefix_cache_hit_rate{model_name="m"} 0.42\n'
        "vllm:gpu_cache_usage_perc 0.1\n"
    ) == {"hit_rate": 0.42, "usage": 0.1}
    assert parse_metrics(
        'sglang:cache_hit_rate{name="x"} 0.8\nsglang:token_usage{name="x"} 0.3\n'
    ) == {"hit_rate": 0.8, "usage": 0.3}
    assert parse_metrics("garbage\n# only comments\n") == {}
    assert metrics_url("http://10.0.0.5:8010/v1") == "http://10.0.0.5:8010/metrics"


class MetricsServer:
    """A local ``/metrics`` endpoint whose prefix-cache counters a test can change."""

    def __init__(self):
        """Start serving in a daemon thread; ``base_url`` points at its ``/v1``."""
        self.queries, self.hits = 1000, 700
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return None

            def do_GET(self):
                if self.path != "/metrics":
                    self.send_response(404)
                    self.end_headers()
                    return
                body = (
                    f'vllm:prefix_cache_queries_total{{model_name="m"}} '
                    f"{outer.queries}\n"
                    f'vllm:prefix_cache_hits_total{{model_name="m"}} {outer.hits}\n'
                    'vllm:kv_cache_usage_perc{model_name="m"} 0.031\n'
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"


class _Log:
    def __init__(self):
        self.records = []

    def log(self, event, agent=None, **payload):
        self.records.append({"event": event, "agent": agent, **payload})


def test_cache_monitor_reports_what_changed_during_the_run():
    """``CacheMonitor`` logs each server's cache hits for this run, errors included."""
    srv = MetricsServer()
    log = _Log()
    mon = CacheMonitor(
        [
            {"base_url": srv.base_url, "model": "m", "role": "reviewer"},
            {
                "base_url": srv.base_url,
                "model": "m",
                "role": "validator",
            },  # same server: one entry
            {"base_url": "http://127.0.0.1:9/v1", "model": "x", "role": "validator"},
        ],
        log,
        interval_s=0.05,
        timeout_s=1,
    ).start()
    try:
        srv.queries, srv.hits = (
            1400,
            1000,
        )  # this run: 300 of 400 prompt tokens from the cache
        deadline = time.time() + 5
        while not any(r["servers"][0].get("run_queries") == 400 for r in log.records):
            assert time.time() < deadline, log.records
            time.sleep(0.05)
    finally:
        srv.httpd.shutdown()
        mon.stop()
    first, last = log.records[0], log.records[-1]
    assert all(r["event"] == "kv_cache" for r in log.records) and last["final"] is True
    up, down = first["servers"]
    assert (
        up["roles"] == ["reviewer", "validator"]
        and up["run_queries"] == 0
        and up["run_hit_rate"] is None
    )
    assert up["usage_perc"] == 3.1 and up["lifetime_hit_rate"] == 0.7
    assert "error" in down and down["server"] == "127.0.0.1:9"
    seen = next(
        r["servers"][0]
        for r in log.records
        if r["servers"][0].get("run_queries") == 400
    )
    assert (seen["run_hits"], seen["run_hit_rate"]) == (300, 0.75)
    n = len(log.records)
    mon.stop()  # stopping twice is harmless
    assert len(log.records) == n


def test_cached_tokens_per_call_when_the_server_reports_them():
    """Per-call cached prompt tokens are reported only when the server provides them."""
    with_details = {
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 40,
            "prompt_tokens_details": {"cached_tokens": 1024},
        }
    }
    assert LocalBackend._usage(with_details) == {
        "input_tokens": 1200,
        "output_tokens": 40,
        "cached_input_tokens": 1024,
    }
    without = {
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 40,
            "prompt_tokens_details": None,
        }
    }
    assert LocalBackend._usage(without) == {
        "input_tokens": 1200,
        "output_tokens": 40,
    }, "no made-up zero when unknown"


def _activity(run_dir):
    return [
        json.loads(line)
        for line in (run_dir / "activity.jsonl").read_text().splitlines()
    ]


def test_memory_used_events_show_what_each_agent_got_and_the_hits(demo_repo, tmp_path):
    """``memory_used`` shows each agent's notes and the validators' past verdicts."""
    from scr_agents.pipeline import run_review

    repo, base, head = demo_repo
    settings = Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "scanners": {"enabled": ["builtin"]},
        }
    )
    _r1, first = run_review(settings, repo, base, head)
    assert not any(
        r["event"] == "memory_used" and r.get("prior") for r in _activity(first)
    ), "nothing to remember yet"
    (repo / ".scr_agents" / "memory.md").write_text(
        "# Notes\nThe pickle loader only reads files we ship.\n"
    )
    _r2, second = run_review(settings, repo, base, head)
    recs = _activity(second)
    used = [r for r in recs if r["event"] == "memory_used"]
    by_agent = {r["agent"]: r for r in used}
    assert (
        by_agent["review_agent"]["notes_chars"] > 0
        and by_agent["review_agent"]["kind"] == "project"
    )
    assert {"subagent:bugs", "subagent:vulnerabilities", "subagent:style"} <= set(
        by_agent
    )
    hits = [r for r in used if r.get("prior")]
    assert hits and all(r["agent"] == f"validator:{r['finding']}" for r in hits)
    assert all(
        p["run_id"] == _r1.run_id and p["verdict"] for r in hits for p in r["prior"]
    )
    start = {r["finding"]: r["seq"] for r in recs if r["event"] == "validate_start"}
    assert all(r["seq"] > start[r["finding"]] for r in hits), (
        "logged by the validator that got the history"
    )


def test_the_lead_agent_says_which_messages_it_saw(tmp_path):
    """The lead agent's ``memory_used`` event lists the chat messages it was given."""
    token = "mem-token"
    settings = Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "projects_dir": str(tmp_path / "out" / "projects"),
            "scanners": {"enabled": ["builtin"]},
            "web": {"token": token, "allowed_hosts": ["testserver"]},
        }
    )

    def records(c, cid):
        with c.stream("GET", f"/api/chats/{cid}/events?follow=0") as r:
            return [
                json.loads(line[6:])
                for line in r.iter_lines()
                if line.startswith("data: ")
            ]

    def wait_replies(c, cid, n):
        deadline = time.time() + 120
        while True:
            recs = records(c, cid)
            replies = [
                r
                for r in recs
                if r.get("event") == "message"
                and r.get("role") == "assistant"
                and not r.get("intro")
            ]
            if len(replies) >= n and not c.get(f"/api/chats/{cid}").json()["busy"]:
                return recs
            assert time.time() < deadline
            time.sleep(0.2)

    with TestClient(
        create_app(settings, token=token), headers={"Authorization": f"Bearer {token}"}
    ) as c:
        cid = c.post(
            "/api/chats/new", json={"demo": True, "text": "review the last commit"}
        ).json()["chat_id"]
        wait_replies(c, cid, 1)
        assert (
            c.post(
                f"/api/chats/{cid}/messages",
                json={"text": "review the last commit again"},
            ).status_code
            == 202
        )
        recs = wait_replies(c, cid, 2)
    first, second = [r for r in recs if r.get("event") == "memory_used"][:2]
    assert first["agent"] == "lead" and first["kind"] == "conversation"
    assert (first["messages_total"], first["messages_used"], first["last_review"]) == (
        0,
        0,
        None,
    )
    assert (
        second["messages_total"] == 2
        and second["messages_used"] == 2
        and second["chars_cap"] == 600
    )
    assert [m["role"] for m in second["used"]] == ["user", "assistant"]
    assert second["used"][0]["preview"] == "review the last commit"
    assert second["last_review"] and second["last_review"].startswith("run-")
