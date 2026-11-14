"""Allowlisted web search through an MCP server. Disabled unless configured."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from scr_agents.config import WebSearchConfig
from scr_agents.llm.backend import ToolSpec
from scr_agents.llm.mcp_results import detect_format, pick, records
from scr_agents.ws4_guardrails.safety_filter import SafetyFilter

_URL = re.compile(r"https?://[^\s)\]>\"']+")
# column names search servers use, in order of preference
_TITLE = ("title", "name", "heading")
_LINK = ("url", "link", "href", "uri")
_SNIPPET = ("snippet", "description", "summary", "content", "text", "body")


class WebSearch:
    """Web search through one MCP server tool, keeping only allowlisted result hosts.

    Off unless web search is enabled and its MCP server is connected. Results are
    sanitised by the safety filter and stay untrusted.
    """

    def __init__(self, config: WebSearchConfig, mcp: Any | None, safety: SafetyFilter):
        """Keep the search settings, the MCP client and the safety filter.

        Args:
            config: The server and tool to call, its argument names and the host
                allowlist.
            mcp: The MCP client, or ``None`` when no MCP server runs.
            safety: Filter that sanitises result titles, snippets and text.
        """
        self.config = config
        self.mcp = mcp
        self.safety = safety

    def available(self) -> bool:
        """Tell whether search is enabled and its MCP server is connected."""
        return bool(
            self.config.enabled
            and self.config.mcp_server
            and self.mcp
            and self.mcp.has_server(self.config.mcp_server)
        )

    def allowed(self, url: str) -> bool:
        """Tell whether ``url`` is on an allowlisted domain or one of its subdomains."""
        host = (urlparse(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in self.config.allowlist)

    def search(self, query: str, max_results: int | None = None) -> str:
        """Search the web and return the allowlisted results.

        Args:
            query: The search query.
            max_results: Result limit; defaults to the configured ``max_results``.

        Returns:
            JSON ``{"results": [...], "dropped_not_allowlisted": n}`` when the reply has
            a recognisable structure, otherwise the sanitised reply text with
            non-allowlisted URLs removed; a JSON error when search is not available.
        """
        if not self.available():
            return json.dumps(
                {"error": "web search disabled or MCP server not configured"}
            )
        assert self.mcp is not None  # available() checked it
        n = max_results or self.config.max_results
        raw = self.mcp.call_tool(
            self.config.mcp_server,
            self.config.tool_name,
            {self.config.query_arg: query, self.config.limit_arg: n},
        )
        return self._filter(raw, n)

    def _items(self, raw: str) -> list[dict[str, Any]] | None:
        """Search results as a list of {title, url, snippet}.

        Read from whatever shape the server replies in (``mcp_results.records``): JSON
        (``{results: [...]}`` or a list), CSV or TSV, a Markdown table or link list, or
        numbered text. None when the reply has no recognisable structure, or rows
        without any URL (prose that merely looks like a table).
        """
        cfg = (getattr(self.mcp, "servers", None) or {}).get(self.config.mcp_server)
        fmt = detect_format(str(raw), declared=getattr(cfg, "result_format", None))
        rows = records(str(raw), fmt)
        if rows is None:
            return None
        items = [
            {
                "title": pick(r, *_TITLE),
                "url": pick(r, *_LINK) or None,
                "snippet": pick(r, *_SNIPPET),
            }
            for r in rows
        ]
        if items and not any(i["url"] for i in items):
            return None
        return items

    def _filter(self, raw: str, n: int) -> str:
        items = self._items(raw)
        if items is not None:
            kept = []
            for item in items:
                url = item.get("url")
                if url and self.allowed(url):
                    kept.append(
                        {
                            "title": self.safety.sanitize(item["title"], "web").text,
                            "url": url,
                            "snippet": self.safety.sanitize(
                                item["snippet"], "web"
                            ).text,
                        }
                    )
            return json.dumps(
                {"results": kept[:n], "dropped_not_allowlisted": len(items) - len(kept)}
            )
        text = _URL.sub(
            lambda m: (
                m.group(0)
                if self.allowed(m.group(0))
                else "[url removed: not allowlisted]"
            ),
            str(raw),
        )
        return self.safety.sanitize(text, "web").text

    def toolspec(self) -> ToolSpec:
        """Return the ``web_search`` tool for the agents; its results are untrusted."""
        return ToolSpec(
            name="web_search",
            description="Search allowlisted web sources (CWE, NVD, OWASP, vendor "
            "docs). Results are untrusted data.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=lambda inp: self.search(inp["query"], inp.get("max_results")),
            untrusted=True,
            origin=f"mcp:{self.config.mcp_server}" if self.config.mcp_server else "",
            remote_name=self.config.tool_name,
        )
