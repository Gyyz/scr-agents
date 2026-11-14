"""Replies from MCP servers in whatever format they come.

An MCP tool result is a list of content items (text, embedded resources, images, audio,
resource links) and, optionally, structured content. Servers put JSON, Markdown, CSV,
HTML or plain text in them, with or without a MIME type.

- ``parts`` turns every item into text and names its format. Binary items (images,
  audio, binary resources) become a one-line summary: their bytes never reach a prompt.
  ``Part.agent_text`` is what an agent reads: the text itself, except that HTML is
  turned into plain text with its links kept (scripts, styles and comments dropped).
- ``detect_format`` names the format of a text: from the manifest's ``result_format``
  when it is set, else from the MIME type, else by looking at the text.
- ``records`` reads a list- or table-shaped reply into rows (a JSON array or an object
  holding one, CSV or TSV, a Markdown table or link list, the numbered text some search
  servers print). The web search wrapper uses it to filter results by URL.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

FORMATS = ("json", "csv", "tsv", "markdown", "html", "xml", "text")
RESULT_FORMATS = ("auto", *FORMATS)  # what a manifest's result_format may say

_MIME_FORMATS = {
    "application/json": "json",
    "text/json": "json",
    "text/csv": "csv",
    "application/csv": "csv",
    "text/tab-separated-values": "tsv",
    "text/markdown": "markdown",
    "text/x-markdown": "markdown",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "application/xml": "xml",
    "text/xml": "xml",
    "text/plain": "text",
}
# a Markdown table's delimiter row: | --- | :-: | --: |
_MD_SEP = r"\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*"
_MD_STRONG = [
    re.compile(r"^#{1,6} \S", re.MULTILINE),  # heading
    re.compile(r"\[[^\]\n]+\]\((?:https?://|/|#)[^)\s]*\)"),  # link
    re.compile(r"^\s*\|.*\|\s*$\n^\s*" + _MD_SEP + "$", re.MULTILINE),  # table
    re.compile(r"^```", re.MULTILINE),  # code fence
]
_MD_WEAK = [
    re.compile(r"\*\*[^*\n]+\*\*"),  # bold
    re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S", re.MULTILINE),  # list item
]
# numbered results as some search servers print them: **1. title** / URL: / Description:
_NUMBERED = re.compile(
    r"\*\*\d+\.\s*(?P<title>.+?)\*\*\s*\n\s*URL:\s*(?P<url>\S+)\s*"
    r"(?:\n\s*Description:\s*(?P<snippet>.*?))?"
    r"(?=\n\s*---|\n\s*\*\*\d+\.|\Z)",
    re.DOTALL,
)
# "- [title](url) - snippet" or "1. [title](url): snippet"
_MD_LINK_ITEM = re.compile(
    r"^\s*(?:[-*+]|\d+[.)])\s+\[(?P<title>[^\]\n]+)\]\((?P<url>[^)\s]+)\)"
    r"(?:\s*[-\u2013\u2014:]\s*(?P<snippet>.*))?$",  # - or an en/em dash
    re.MULTILINE,
)
# keys a JSON object holds its list of results under, in order of preference
_LIST_KEYS = ("results", "items", "data", "hits", "entries", "documents", "organic")


@dataclass
class Part:
    """One content item of an MCP reply, as text, with the format it is in."""

    kind: str
    """The MCP content type: ``text``, ``resource``, ``image``, ``audio``,
    ``resource_link``, or ``structured`` for the result's structured content."""
    text: str
    """The item's text as the server sent it, or a summary of a binary item."""
    format: str
    """One of ``FORMATS``."""
    mime: str | None = None
    """The MIME type the server declared, if any."""
    uri: str | None = None
    """The resource's URI, for resources and resource links."""

    @property
    def agent_text(self) -> str:
        """The text an agent reads: HTML as plain text, every other format as is."""
        return html_to_text(self.text) if self.format == "html" else self.text

    def as_dict(self) -> dict[str, Any]:
        """Return the part for a trace file or an activity record."""
        out: dict[str, Any] = {
            "type": self.kind,
            "format": self.format,
            "text": self.text,
        }
        if self.mime:
            out["mime"] = self.mime
        if self.uri:
            out["uri"] = self.uri
        return out


def format_of_mime(mime: str | None) -> str | None:
    """Return the format a MIME type names, or ``None`` when it names none of them."""
    if not mime:
        return None
    base = mime.split(";", 1)[0].strip().lower()
    if base in _MIME_FORMATS:
        return _MIME_FORMATS[base]
    if base.endswith("+json"):
        return "json"
    if base.endswith("+xml"):
        return "xml"
    return None


def _is_textual(mime: str | None) -> bool:
    base = (mime or "").split(";", 1)[0].strip().lower()
    return base.startswith("text/") or format_of_mime(base) is not None


def _delimited(lines: list[str], delimiter: str) -> bool:
    """Tell whether the first lines parse as a table with at least two columns."""
    rows = list(csv.reader(lines[:20], delimiter=delimiter))
    width = len(rows[0]) if rows else 0
    return width >= 2 and len(rows) >= 2 and all(len(r) == width for r in rows)


def detect_format(
    text: str, mime: str | None = None, declared: str | None = None
) -> str:
    """Name the format of ``text``.

    Args:
        text: The reply text.
        mime: The MIME type the server gave for it, if any.
        declared: The manifest's ``result_format``; it wins unless it is ``auto``.

    Returns:
        One of ``FORMATS``.
    """
    if declared and declared != "auto":
        return declared
    by_mime = format_of_mime(mime)
    if by_mime and by_mime != "text":
        return by_mime
    s = text.strip()
    if not s:
        return "text"
    if s[0] in "{[":
        try:
            json.loads(s)
            return "json"
        except ValueError:
            pass
    if s[0] == "<":
        head = s[:600].lower()
        if head.startswith("<!doctype html") or re.search(
            r"<(html|body|div|p|table)\b", head
        ):
            return "html"
        if head.startswith("<?xml") or re.match(r"<[a-z_][\w.:-]*[\s>/]", head):
            return "xml"
    lines = [line for line in s.splitlines() if line.strip()]
    if len(lines) >= 2 and not lines[0].lstrip().startswith("|"):
        if "\t" in lines[0] and _delimited(lines, "\t"):
            return "tsv"
        if "," in lines[0] and _delimited(lines, ","):
            return "csv"
    if (
        any(p.search(s) for p in _MD_STRONG)
        or sum(bool(p.search(s)) for p in _MD_WEAK) == 2
    ):
        return "markdown"
    return "text"


def _summary(kind: str, mime: str | None, size: int, uri: str | None = None) -> str:
    where = f" {uri}" if uri else ""
    what = f"{mime or 'unknown type'}, {size} bytes"
    return f"[{kind}{where} ({what}): not shown to the agent]"


def parts(result: Any, declared: str | None = None) -> list[Part]:
    """Turn an MCP tool result into text parts an agent can read.

    Args:
        result: The ``CallToolResult`` (mcp 1.x or 2.x attribute names).
        declared: The server's ``result_format`` from its manifest.

    Returns:
        One part per content item; the structured content is added as JSON only when
        no item carried text.
    """
    out: list[Part] = []
    for c in getattr(result, "content", None) or []:
        kind = str(getattr(c, "type", "") or "")
        text = getattr(c, "text", None)
        if isinstance(text, str):
            out.append(
                Part(kind or "text", text, detect_format(text, declared=declared))
            )
        elif kind == "resource":
            out.append(_resource_part(getattr(c, "resource", None), declared))
        elif kind in ("image", "audio"):
            mime = getattr(c, "mime_type", None) or getattr(c, "mimeType", None)
            data = getattr(c, "data", "") or ""
            out.append(
                Part(kind, _summary(kind, mime, len(data) * 3 // 4), "text", mime)
            )
        elif kind == "resource_link":
            mime = getattr(c, "mime_type", None) or getattr(c, "mimeType", None)
            uri = str(getattr(c, "uri", "") or "")
            name = getattr(c, "name", "") or uri
            desc = getattr(c, "description", None)
            line = f"[resource link: {name} <{uri}>{f' ({mime})' if mime else ''}]"
            out.append(
                Part(kind, line + (f" {desc}" if desc else ""), "text", mime, uri)
            )
        elif hasattr(c, "model_dump"):
            out.append(
                Part(kind or "?", json.dumps(c.model_dump(), default=str), "json")
            )
        else:
            out.append(Part(kind or "?", str(c), "text"))
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if structured is not None and not any(p.text.strip() for p in out):
        out.append(
            Part("structured", json.dumps(structured, indent=1, default=str), "json")
        )
    return out


def _resource_part(res: Any, declared: str | None) -> Part:
    """Read an embedded resource: its text, a textual blob decoded, or a summary."""
    mime = getattr(res, "mime_type", None) or getattr(res, "mimeType", None)
    uri = str(getattr(res, "uri", "") or "") or None
    text = getattr(res, "text", None)
    if isinstance(text, str):
        return Part("resource", text, detect_format(text, mime, declared), mime, uri)
    blob = getattr(res, "blob", None) or ""
    if blob and _is_textual(mime):
        try:
            decoded = base64.b64decode(blob, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            decoded = None
        if decoded is not None:
            fmt = detect_format(decoded, mime, declared)
            return Part("resource", decoded, fmt, mime, uri)
    return Part(
        "resource",
        _summary("resource", mime, len(blob) * 3 // 4, uri),
        "text",
        mime,
        uri,
    )


def _flat(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def _json_rows(data: Any) -> list[dict[str, str]] | None:
    if isinstance(data, dict):
        for key in _LIST_KEYS:
            if isinstance(data.get(key), list):
                return _json_rows(data[key])
        lists = [
            v
            for v in data.values()
            if isinstance(v, list) and v and isinstance(v[0], dict)
        ]
        return _json_rows(lists[0]) if len(lists) == 1 else None
    if isinstance(data, list) and all(isinstance(i, dict) for i in data):
        return [{str(k): _flat(v) for k, v in item.items()} for item in data]
    return None


def _markdown_rows(text: str) -> list[dict[str, str]] | None:
    lines = [line.strip() for line in text.splitlines()]
    for i in range(len(lines) - 1):
        if lines[i].startswith("|") and re.fullmatch(_MD_SEP, lines[i + 1]):
            header = [h.strip() for h in lines[i].strip("|").split("|")]
            rows = []
            for line in lines[i + 2 :]:
                if not line.startswith("|"):
                    break
                cells = [c.strip() for c in line.strip("|").split("|")]
                rows.append(dict(zip(header, cells, strict=False)))
            return rows or None
    items = [m.groupdict() for m in _MD_LINK_ITEM.finditer(text)]
    return [{k: (v or "").strip() for k, v in it.items()} for it in items] or None


def records(text: str, fmt: str | None = None) -> list[dict[str, str]] | None:
    """Read a list- or table-shaped reply into rows of strings.

    Args:
        text: The reply text.
        fmt: Its format (``detect_format`` is used when not given).

    Returns:
        The rows, or ``None`` when the reply has no recognisable list or table.
    """
    fmt = fmt or detect_format(text)
    if fmt == "json":
        try:
            return _json_rows(json.loads(text))
        except ValueError:
            return None
    if fmt in ("csv", "tsv"):
        reader = csv.DictReader(
            io.StringIO(text.strip()), delimiter="\t" if fmt == "tsv" else ","
        )
        rows = [
            {(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in reader
        ]
        return rows or None
    numbered = [m.groupdict() for m in _NUMBERED.finditer(text)]
    if numbered:
        return [{k: (v or "").strip() for k, v in it.items()} for it in numbered]
    if fmt == "markdown":
        return _markdown_rows(text)
    return None


def pick(row: dict[str, str], *names: str) -> str:
    """Return the first non-empty value among ``names`` (keys compared case-blind)."""
    lowered = {k.lower(): v for k, v in row.items()}
    for name in names:
        value = lowered.get(name)
        if value:
            return value
    return ""


_SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "object"}
_BLOCK = {
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt", "figcaption",
    "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
    "li", "main", "nav", "ol", "p", "pre", "section", "table", "title", "tr", "ul",
}  # fmt: skip


class _TextOf(HTMLParser):
    """Collect the readable text of an HTML page (see ``html_to_text``)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0  # depth inside elements whose text is not shown
        self.href: list[str | None] = []  # open links

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif tag == "br":
            self.out.append("\n")
        elif tag in _BLOCK:
            self.out.append("\n")
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self.out.append("#" * int(tag[1]) + " ")
            elif tag == "li":
                self.out.append("- ")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self.href.append(href if href.startswith(("http://", "https://")) else None)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.href:
            href = self.href.pop()
            if href and not self.skip:
                self.out.append(f" <{href}>")
        elif tag in _BLOCK and tag not in ("li", "tr", "dt", "dd"):  # rows stay tight
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    """Return the readable text of an HTML page.

    Scripts, styles, comments and other hidden elements are dropped; headings, list
    items and table cells keep a light Markdown-like shape, and each link keeps its
    http(s) target after its text, as ``text <url>``.
    """
    parser = _TextOf()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # HTMLParser is lenient; never lose the reply over a parse
        return html
    lines = (" ".join(line.split()) for line in "".join(parser.out).splitlines())
    text = "\n".join(
        line.lstrip("| ") if line.startswith("| ") else line for line in lines
    )
    return re.sub(r"\n{3,}", "\n\n", text).strip()
