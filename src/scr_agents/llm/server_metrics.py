"""Prefix (KV) cache statistics of the local model servers, from Prometheus `/metrics`.

vLLM (V1) counts prompt tokens looked up in its prefix cache and the tokens found there
(`vllm:prefix_cache_queries_total` / `vllm:prefix_cache_hits_total`) and reports how
full the KV cache is (`vllm:kv_cache_usage_perc`); older vLLM and SGLang publish a ready
hit rate and a usage ratio instead. The counters are server-wide: during a run they
include every other client of the same server. Per-request numbers
(`usage.prompt_tokens_details.cached_tokens`, read by `LocalBackend`) need vLLM's
`--enable-prompt-tokens-details` (SGLang: `--enable-cache-report`).

`CacheMonitor` samples every configured server while a run is going and logs `kv_cache`
events with what changed since the run started; it never raises into the run.
"""

from __future__ import annotations

import re
import threading
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

# family -> metric names, first found wins (vLLM V1, older vLLM, SGLang)
FAMILIES: dict[str, tuple[str, ...]] = {
    "queries": ("vllm:prefix_cache_queries_total", "vllm:prefix_cache_queries"),
    "hits": ("vllm:prefix_cache_hits_total", "vllm:prefix_cache_hits"),
    "usage": (
        "vllm:kv_cache_usage_perc",
        "vllm:gpu_cache_usage_perc",
        "sglang:token_usage",
    ),
    "hit_rate": ("vllm:gpu_prefix_cache_hit_rate", "sglang:cache_hit_rate"),
}
_LINE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(\{[^}]*\})?\s+(\S+)")
_MODEL_LABEL = re.compile(r'model_name="([^"]*)"')


def parse_metrics(text: str, model: str | None = None) -> dict[str, float]:
    """Sum each family over its series (one per engine).

    Keeps only `model` when the series name it.
    """
    found: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        name, labels, value = m.groups()
        if model and labels:
            lm = _MODEL_LABEL.search(labels)
            if lm and lm.group(1) != model:
                continue
        try:
            v = float(value)
        except ValueError:
            continue
        found.setdefault(name, {})
        found[name][labels or ""] = found[name].get(labels or "", 0.0) + v
    out: dict[str, float] = {}
    for family, names in FAMILIES.items():
        for name in names:
            if name in found:
                vals = list(found[name].values())
                # ratios are averaged over engines, counters summed
                out[family] = (
                    sum(vals) / len(vals)
                    if family in ("usage", "hit_rate")
                    else sum(vals)
                )
                break
    return out


def metrics_url(base_url: str) -> str:
    """http://host:8010/v1 -> http://host:8010/metrics.

    The server root, not the API prefix.
    """
    p = urlparse(base_url.rstrip("/"))
    return f"{p.scheme}://{p.netloc}/metrics"


def fetch(base_url: str, api_key: str = "", timeout: float = 3.0) -> str:
    """Download the Prometheus text from the ``/metrics`` of the server at ``base_url``.

    ``api_key`` is sent as a bearer token unless it is empty or ``EMPTY``; at most 4 MB
    of the response is read.

    Raises:
        OSError: The server could not be reached or answered with an error status
            (``urllib.error.URLError`` is an ``OSError``).
    """
    headers = {"Accept": "text/plain"}
    if api_key and api_key != "EMPTY":
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(metrics_url(base_url), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data: bytes = resp.read(4_000_000)
        return data.decode("utf-8", "replace")


class CacheMonitor:
    """Samples the servers' cache metrics while a run is going.

    Logs `kv_cache` when something changed.
    """

    def __init__(
        self,
        servers: list[dict[str, str]],
        log: Any,
        api_key: str = "",
        interval_s: float = 5.0,
        timeout_s: float = 3.0,
    ):
        """Set up the monitor; sampling starts with ``start``.

        Args:
            servers: One dict per role, with ``base_url`` and optionally ``model`` and
                ``role``; entries with the same endpoint and model are merged.
            log: The run's activity log; ``kv_cache`` events go to its ``log`` method.
            api_key: Bearer token for ``/metrics``, if the servers need one.
            interval_s: Seconds between samples.
            timeout_s: Timeout of each fetch, in seconds.
        """
        # one entry per server endpoint; roles that share it are merged ("reviewer",
        # "validator")
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for s in servers:
            key = (s["base_url"].rstrip("/"), s.get("model", ""))
            entry = merged.setdefault(
                key, {"base_url": key[0], "model": key[1], "roles": []}
            )
            if s.get("role") and s["role"] not in entry["roles"]:
                entry["roles"].append(s["role"])
        self.servers = list(merged.values())
        self.log = log
        self.api_key = api_key
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self._baseline: dict[str, dict[str, float] | None] = {}
        self._last: list[dict[str, Any]] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self, server: dict[str, Any]) -> dict[str, float] | str:
        try:
            return parse_metrics(
                fetch(server["base_url"], self.api_key, self.timeout_s),
                server["model"] or None,
            )
        except (OSError, urllib.error.URLError, ValueError) as e:
            return f"{type(e).__name__}: {e}"

    def snapshot(self) -> list[dict[str, Any]]:
        """Sample every server now and describe its cache, one dict per server.

        The first successful sample of a server is its baseline, so the ``run_*``
        fields count only what happened since then. A server that cannot be read, or
        publishes none of the known metrics, gets an ``error`` field instead.
        """
        out = []
        for s in self.servers:
            url = s["base_url"]
            entry: dict[str, Any] = {
                "server": urlparse(url).netloc,
                "model": s["model"],
                "roles": s["roles"],
            }
            cur = self._sample(s)
            if isinstance(cur, str):
                entry["error"] = cur
                out.append(entry)
                continue
            base = self._baseline.get(url)
            if url not in self._baseline:
                self._baseline[url] = base = dict(cur)
            if "usage" in cur:
                entry["usage_perc"] = (
                    round(cur["usage"] * 100, 1)
                    if cur["usage"] <= 1
                    else round(cur["usage"], 1)
                )
            if "queries" in cur and "hits" in cur:
                entry["lifetime_queries"], entry["lifetime_hits"] = (
                    int(cur["queries"]),
                    int(cur["hits"]),
                )
                entry["lifetime_hit_rate"] = (
                    round(cur["hits"] / cur["queries"], 4) if cur["queries"] else None
                )
                if base and "queries" in base:
                    q, h = (
                        int(cur["queries"] - base["queries"]),
                        int(cur["hits"] - base.get("hits", 0)),
                    )
                    entry["run_queries"], entry["run_hits"] = q, h
                    entry["run_hit_rate"] = round(h / q, 4) if q > 0 else None
            elif "hit_rate" in cur:
                entry["hit_rate"] = round(cur["hit_rate"], 4)
            if not any(
                k in entry for k in ("usage_perc", "lifetime_queries", "hit_rate")
            ):
                entry["error"] = "no prefix-cache metrics on this server's /metrics"
            out.append(entry)
        return out

    def _emit(self, final: bool = False) -> None:
        try:
            snap = self.snapshot()
            changed = [
                {k: v for k, v in s.items() if k != "usage_perc"} for s in snap
            ] != [
                {k: v for k, v in s.items() if k != "usage_perc"}
                for s in (self._last or [])
            ]
            if final or changed or self._last is None:
                self.log.log("kv_cache", None, servers=snap, final=final)
            self._last = snap
        except Exception:
            pass

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._emit()

    def start(self) -> CacheMonitor:
        """Log the baseline and keep sampling in a daemon thread; returns ``self``.

        Does nothing when there are no servers to watch.
        """
        if not self.servers:
            return self
        self._emit()  # baseline
        self._thread = threading.Thread(
            target=self._loop, name="kv-cache-monitor", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop the sampling thread and log a final ``kv_cache`` event."""
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=self.timeout_s + 2)
        self._thread = None
        self._emit(final=True)
