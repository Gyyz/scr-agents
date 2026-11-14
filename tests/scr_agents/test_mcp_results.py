"""MCP replies in any format: JSON, CSV, TSV, Markdown, HTML, XML, text.

Covers format detection, every MCP content type, reading results into rows, the web
search wrapper on each result shape, and a real stdio server replying in several.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from scr_agents.config import MCPServerConfig, ResultFormat, WebSearchConfig
from scr_agents.llm import mcp_results
from scr_agents.llm.mcp_client import MCPClient
from scr_agents.llm.mcp_registry import MCPManifest
from scr_agents.llm.mcp_results import detect_format, html_to_text, parts, records
from scr_agents.ws4_guardrails.safety_filter import SafetyFilter
from scr_agents.ws4_guardrails.web_search import WebSearch

SERVER = Path(__file__).parent / "mcp_formats_server.py"
CWE78 = "https://cwe.mitre.org/data/definitions/78.html"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"results": []}', "json"),
        ("[1, 2]", "json"),
        ("title,url\nCWE-78,https://cwe.mitre.org\nCWE-89,https://owasp.org", "csv"),
        ("title\turl\nCWE-78\thttps://cwe.mitre.org", "tsv"),
        ("# Findings\n\nOne issue.", "markdown"),
        ("| a | b |\n|---|---|\n| 1 | 2 |", "markdown"),
        ("See [CWE-78](https://cwe.mitre.org/x) for details.", "markdown"),
        ("- **one** item\n- two", "markdown"),
        ("<!DOCTYPE html><html><body>x</body></html>", "html"),
        ("<p>hello</p>", "html"),
        ('<?xml version="1.0"?><r/>', "xml"),
        ("<result><cwe>78</cwe></result>", "xml"),
        ("Hello, world.", "text"),
        ("{not json", "text"),
        ("", "text"),
    ],
)
def test_the_format_is_recognised_from_the_content(text, expected):
    """With no MIME type and nothing declared, the format comes from the text."""
    assert detect_format(text) == expected


def test_a_mime_type_or_a_declared_format_wins_over_the_content():
    """The manifest's ``result_format`` beats the MIME type, which beats sniffing."""
    assert detect_format("a,b\n1,2", "text/markdown") == "markdown"
    assert detect_format("a,b\n1,2", "application/vnd.api+json") == "json"
    assert detect_format("# not a heading", "text/plain") == "markdown", (
        "text/plain says nothing about the format: sniff it"
    )
    assert detect_format('{"a": 1}', "application/json", declared="text") == "text"
    assert detect_format('{"a": 1}', declared="auto") == "json"


def test_the_formats_named_in_the_config_are_the_ones_the_module_knows():
    """``ResultFormat`` in config.py and ``RESULT_FORMATS`` list the same names."""
    assert set(ResultFormat.__args__) == set(mcp_results.RESULT_FORMATS)


def test_every_content_type_becomes_text_without_binary_data():
    """Images, audio and binary resources become a summary; their bytes stay out."""
    png = base64.b64encode(b"\x89PNG" + b"\x00" * 300).decode()
    md = base64.b64encode(b"# Notes\n\n- one").decode()
    result = NS(
        content=[
            NS(type="text", text="a,b\n1,2"),
            NS(type="image", data=png, mime_type="image/png"),
            NS(type="audio", data="AAAA", mimeType="audio/wav"),  # mcp 1.x name
            NS(
                type="resource",
                resource=NS(uri="file:///r.csv", mime_type="text/csv", text="x;y"),
            ),
            NS(
                type="resource",
                resource=NS(uri="file:///n.md", mime_type="text/markdown", blob=md),
            ),
            NS(
                type="resource",
                resource=NS(uri="file:///b.bin", mime_type="image/png", blob=png),
            ),
            NS(type="resource_link", uri="file:///big.log", name="big.log",
               mime_type="text/plain", description="the full build log"),
        ],
        structured_content={"ignored": "the items carry text"},
    )  # fmt: skip
    got = parts(result)
    assert [(p.kind, p.format) for p in got] == [
        ("text", "csv"),
        ("image", "text"),
        ("audio", "text"),
        ("resource", "csv"),  # the MIME type says CSV even if the content does not
        ("resource", "markdown"),  # a textual blob is decoded
        ("resource", "text"),
        ("resource_link", "text"),
    ]
    assert png not in "".join(p.text for p in got), "binary data never reaches a prompt"
    assert got[1].text.startswith("[image (image/png, ") and "not shown" in got[1].text
    assert got[2].mime == "audio/wav"
    assert got[4].text == "# Notes\n\n- one"
    assert got[5].text.startswith("[resource file:///b.bin (image/png, ")
    assert got[6].text == (
        "[resource link: big.log <file:///big.log> (text/plain)] the full build log"
    )
    assert not any(p.kind == "structured" for p in got)


def test_structured_content_is_used_only_when_no_item_has_text():
    """A reply carrying only structured content is shown as JSON."""
    got = parts(NS(content=[], structured_content={"cwe": 78}))
    assert [(p.kind, p.format) for p in got] == [("structured", "json")]
    assert json.loads(got[0].text) == {"cwe": 78}
    assert parts(NS(content=None)) == []


def test_html_reaches_the_agent_as_text_with_its_links():
    """HTML loses scripts, styles and comments, and keeps its text and link targets."""
    html = (
        "<html><head><title>CWE-89</title><style>p{color:red}</style>"
        "<script>steal()</script></head><body><h2>SQL injection</h2>"
        '<p>See <a href="https://cwe.mitre.org/x">CWE-89</a> &amp; '
        '<a href="javascript:x()">this</a>.<!-- ignore previous --></p>'
        "<ul><li>one</li><li>two</li></ul>"
        "<table><tr><th>id</th><th>name</th></tr><tr><td>89</td><td>SQLi</td></tr>"
        "</table></body></html>"
    )
    text = html_to_text(html)
    assert text == (
        "CWE-89\n\n## SQL injection\n\nSee CWE-89 <https://cwe.mitre.org/x> & this.\n\n"
        "- one\n- two\n\nid | name\n89 | SQLi"
    )
    (part,) = parts(NS(content=[NS(type="text", text=html)]))
    assert part.format == "html" and part.text == html and part.agent_text == text


@pytest.mark.parametrize(
    "reply",
    [
        json.dumps({"results": [{"title": "CWE-78", "url": CWE78, "snippet": "s"}]}),
        json.dumps({"data": {"n": 1}, "items": [{"name": "CWE-78", "link": CWE78}]}),
        json.dumps([{"Title": "CWE-78", "URL": CWE78}]),
        f"title,url,snippet\nCWE-78,{CWE78},\"s, with a comma\"\n",
        f"name\thref\nCWE-78\t{CWE78}\n",
        f"Results:\n\n| Title | URL |\n|:--|--:|\n| CWE-78 | {CWE78} |\n\nend",
        f"Results:\n\n- [CWE-78]({CWE78}) - s\n- [Other](https://owasp.org/x)",
        f"**1. CWE-78**\nURL: {CWE78}\nDescription: s\n\n---\n",
    ],
    ids=["json", "json-items", "json-list", "csv", "tsv", "md-table", "md-links",
         "numbered"],
)  # fmt: skip
def test_list_shaped_replies_are_read_into_rows(reply):
    """Every list or table shape gives rows with the first result's title and URL."""
    rows = records(reply)
    assert rows, reply
    first = {k.lower(): v for k, v in rows[0].items()}
    assert CWE78 in first.values()
    assert "CWE-78" in first.values()


def test_replies_without_a_list_have_no_rows():
    """Prose, a JSON object without a list, or HTML give no rows."""
    assert records("Nothing found.") is None
    assert records('{"error": "rate limited"}') is None
    assert records("<p>x</p>") is None
    assert records('{"results": []}') == []


class _FakeMCP:
    def __init__(self, reply: str, result_format: str = "auto"):
        self.reply = reply
        self.servers = {
            "websearch": MCPServerConfig(
                name="websearch", command="x", result_format=result_format
            )
        }

    def has_server(self, name: str) -> bool:
        return name in self.servers

    def call_tool(self, server: str, name: str, args: dict) -> str:
        return self.reply


def _search(reply: str, result_format: str = "auto") -> str:
    config = WebSearchConfig(
        enabled=True,
        mcp_server="websearch",
        allowlist=["cwe.mitre.org", "owasp.org"],
    )
    ws = WebSearch(config, _FakeMCP(reply, result_format), SafetyFilter())
    return ws.search("CWE-78")


@pytest.mark.parametrize(
    "reply",
    [
        "title,url,snippet\n"
        f"CWE-78,{CWE78},OS command injection\n"
        "Blog,https://evil.example.net/post,sketchy\n",
        f"| Title | Link | Description |\n|---|---|---|\n| CWE-78 | {CWE78} | OS "
        "command injection |\n| Blog | https://evil.example.net/post | sketchy |",
        f"- [CWE-78]({CWE78}): OS command injection\n"
        "- [Blog](https://evil.example.net/post): sketchy",
    ],
    ids=["csv", "md-table", "md-links"],
)
def test_web_search_allowlists_results_in_any_format(reply):
    """CSV, Markdown tables and Markdown link lists are filtered per result."""
    out = json.loads(_search(reply))
    assert out["results"] == [
        {"title": "CWE-78", "url": CWE78, "snippet": "OS command injection"}
    ]
    assert out["dropped_not_allowlisted"] == 1


def test_web_search_treats_rows_without_urls_as_text():
    """Prose that looks like CSV is not mistaken for results with no URL."""
    reply = (
        "Found it, see below\n"
        f"Details, {CWE78}\n"
        "Elsewhere, https://evil.example.net/post"
    )
    out = _search(reply)
    assert CWE78 in out and "evil.example.net" not in out
    assert "[url removed: not allowlisted]" in out


def test_web_search_follows_the_declared_format():
    """A server declared as text is never read as a table."""
    reply = f"title,url\nCWE-78,{CWE78}\nBlog,https://evil.example.net/post"
    assert json.loads(_search(reply))["dropped_not_allowlisted"] == 1
    assert _search(reply, result_format="text") == (
        f"title,url\nCWE-78,{CWE78}\nBlog,[url removed: not allowlisted]"
    ), "read as text: URLs scrubbed in place, no JSON"


def test_a_manifest_can_declare_the_format():
    """``result_format`` in mcp.yaml reaches the server config; unknown names fail."""
    m = MCPManifest(name="x", command="x", result_format="csv")
    assert m.result_format == "csv"
    with pytest.raises(ValueError, match="result_format"):
        MCPManifest(name="x", command="x", result_format="pdf")


class _Log:
    def __init__(self):
        self.records, self.traces = [], {}

    def log(self, event, agent=None, **payload):
        self.records.append({"event": event, "agent": agent, **payload})

    def write_trace(self, name, data):
        self.traces[name] = data
        return f"trace/{name}.json"


@pytest.mark.skipif(not MCPClient.available(), reason="mcp extra not installed")
def test_a_real_server_replying_in_several_formats():
    """CSV, HTML, an image and an embedded resource reach the agent as text.

    The activity log names each reply's format and the trace keeps the HTML source.
    """
    log = _Log()
    client = MCPClient(
        [MCPServerConfig(name="fmt", command=sys.executable, args=[str(SERVER)])],
        connect_timeout=20,
        log=log,
        safety=SafetyFilter(),
    ).start()
    try:
        assert client.has_server("fmt"), client.errors
        csv_text = client.call_tool("fmt", "table", {})
        assert csv_text.startswith("title,url,snippet\nCWE-78,")
        page = client.call_tool("fmt", "page", {})
        assert page == (
            "CWE-89\n\n# SQL injection\n\n"
            "See CWE-89 <https://cwe.mitre.org/data/definitions/89.html>."
        )
        picture = client.call_tool("fmt", "picture", {})
        assert picture.startswith("[image (image/png, 72 bytes)")
        report = client.call_tool("fmt", "report", {})
        assert report == "1 finding\n# Report\n\n- **CWE-78** in `run.py`"
    finally:
        client.close()

    calls = {r["tool"]: r for r in log.records if r["event"] == "mcp_call"}
    assert calls["table"]["format"] == "csv"
    assert calls["page"]["format"] == "html"
    assert "<script>" in calls["page"]["output_preview"], "the log shows the source"
    assert calls["picture"]["parts"] == [
        {"type": "image", "format": "text", "mime": "image/png", "chars": len(picture)}
    ]
    assert calls["report"]["format"] == "mixed"
    assert [p["format"] for p in calls["report"]["parts"]] == ["text", "markdown"]
    trace = log.traces["mcp-fmt-report"]["response"]["content"]
    assert trace[1] == {
        "type": "resource",
        "format": "markdown",
        "text": "# Report\n\n- **CWE-78** in `run.py`",
        "mime": "text/markdown",
        "uri": "file:///report.md",
    }
