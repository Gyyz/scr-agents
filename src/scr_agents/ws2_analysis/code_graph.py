"""Code graph: symbol definitions, reference edges and the blast radius of a change.

Python is parsed with `ast`; other languages use regex-based definition extraction
plus word-boundary reference search. Good enough to tell the agents what else a change
touches.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scr_agents.inputs.languages import CODE_LANGS, detect_language
from scr_agents.models import ChangedFile, GraphReport, SymbolRef

MAX_FILES = 4000
MAX_FILE_BYTES = 400_000
MAX_TOTAL_BYTES = 60_000_000
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    "target",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "vendor",
    "third_party",
}

_KEYWORDS = {
    "if",
    "for",
    "while",
    "switch",
    "return",
    "else",
    "do",
    "sizeof",
    "case",
    "catch",
    "try",
    "new",
    "delete",
    "throw",
    "foreach",
    "elif",
    "except",
    "with",
    "defer",
}

DEF_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "c": [
        re.compile(
            r"^(?!\s*(?:if|for|while|switch|return|else|do|sizeof|case)\b)[ \t]*"
            r"(?:[\w\*&:<>,\[\]]+[ \t\*&]+)+([A-Za-z_]\w*)\s*\([^;)]*\)"
            r"\s*(?:const)?\s*\{",
            re.MULTILINE,
        )
    ],
    "go": [re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(", re.MULTILINE)],
    "rust": [
        re.compile(
            r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+"
            r"([A-Za-z_]\w*)",
            re.MULTILINE,
        )
    ],
    "javascript": [
        re.compile(
            r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(",
            re.MULTILINE,
        ),
        re.compile(
            r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
            r"(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>",
            re.MULTILINE,
        ),
        re.compile(
            r"^\s{2,}(?:static\s+)?(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{",
            re.MULTILINE,
        ),
    ],
    "java": [
        re.compile(
            r"^(?!\s*(?:if|for|while|switch|return|else|do|case|new|catch|try)\b)[ \t]*"
            r"(?:[\w<>\[\],\s]+\s+)+([A-Za-z_]\w*)\s*\([^;)]*\)\s*(?:throws[^{]*)?\{",
            re.MULTILINE,
        )
    ],
    "ruby": [re.compile(r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[?!=]?)", re.MULTILINE)],
    "php": [re.compile(r"function\s+&?([A-Za-z_]\w*)\s*\(", re.MULTILINE)],
    "swift": [re.compile(r"\bfunc\s+([A-Za-z_]\w*)", re.MULTILINE)],
    "kotlin": [
        re.compile(
            r"\bfun\s+(?:<[^>]+>\s+)?(?:[\w.]+\.)?([A-Za-z_]\w*)\s*\(", re.MULTILINE
        )
    ],
    "shell": [
        re.compile(r"^\s*(?:function\s+)?([A-Za-z_]\w*)\s*\(\)\s*\{", re.MULTILINE)
    ],
}
DEF_PATTERNS["cpp"] = DEF_PATTERNS["c"]
DEF_PATTERNS["typescript"] = DEF_PATTERNS["javascript"]
DEF_PATTERNS["csharp"] = DEF_PATTERNS["java"]


@dataclass
class Symbol:
    """A function or class definition, with the names it calls.

    ``line`` and ``end_line`` are 1-based and inclusive. Python methods and
    nested definitions are named with their enclosing names (``Class.method``).
    """

    name: str
    path: str
    line: int
    end_line: int
    kind: str = "function"
    calls: set[str] = field(default_factory=set)

    def ref(self) -> SymbolRef:
        """Return the ``SymbolRef`` that reports and tools carry for this symbol."""
        return SymbolRef(name=self.name, path=self.path, line=self.line, kind=self.kind)


class CodeGraph:
    """Symbol definitions and call sites of a source tree, for blast-radius queries."""

    def __init__(self, root: Path):
        """Create an empty graph over ``root``; ``build`` returns a filled one."""
        self.root = Path(root)
        self.files: dict[str, str] = {}
        self.symbols: dict[str, list[Symbol]] = {}
        self.by_name: dict[str, list[Symbol]] = {}

    # building --------------------------------------------------------------
    @classmethod
    def build(cls, root: Path, paths: list[str] | None = None) -> CodeGraph:
        """Read the source files under ``root`` and index their definitions.

        Files that are not source code are skipped. At most
        ``MAX_FILES`` files are considered; a file over ``MAX_FILE_BYTES``, or one
        that would take the total past ``MAX_TOTAL_BYTES``, is skipped.

        Args:
            root: The checkout to read.
            paths: Files to index, relative to ``root``. ``None`` walks the whole
                tree, leaving out ``SKIP_DIRS``.

        Returns:
            The filled graph.
        """
        g = cls(root)
        total = 0
        candidates = paths or g._walk()
        for rel in candidates[:MAX_FILES]:
            p = g.root / rel
            lang = detect_language(rel)
            if lang not in CODE_LANGS or not p.is_file():
                continue
            try:
                size = p.stat().st_size
                if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            total += size
            g.files[rel] = text
            g.symbols[rel] = g._extract(rel, text, lang)
            for s in g.symbols[rel]:
                g.by_name.setdefault(s.name.split(".")[-1], []).append(s)
        return g

    def _walk(self) -> list[str]:
        out: list[str] = []
        for p in sorted(self.root.rglob("*")):
            if any(part in SKIP_DIRS for part in p.relative_to(self.root).parts):
                continue
            if p.is_file():
                out.append(p.relative_to(self.root).as_posix())
        return out

    def _extract(self, rel: str, text: str, lang: str) -> list[Symbol]:
        if lang == "python":
            return self._extract_python(rel, text)
        syms: list[Symbol] = []
        lines = text.splitlines()
        seen: set[tuple[str, int]] = set()
        for pat in DEF_PATTERNS.get(lang, []):
            for m in pat.finditer(text):
                name = m.group(1)
                if name in _KEYWORDS:
                    continue
                line = text.count("\n", 0, m.start()) + 1
                if (name, line) in seen:
                    continue
                seen.add((name, line))
                end = _brace_end(lines, line - 1) if lang != "ruby" else line
                body = "\n".join(lines[line - 1 : end])
                calls = {
                    c
                    for c in re.findall(r"\b([A-Za-z_]\w*)\s*\(", body)
                    if c != name and c not in _KEYWORDS
                }
                syms.append(
                    Symbol(name=name, path=rel, line=line, end_line=end, calls=calls)
                )
        syms.sort(key=lambda s: s.line)
        return syms

    def _extract_python(self, rel: str, text: str) -> list[Symbol]:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        syms: list[Symbol] = []

        def visit(node: ast.AST, prefix: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    name = f"{prefix}{child.name}"
                    kind = "class" if isinstance(child, ast.ClassDef) else "function"
                    calls: set[str] = set()
                    for sub in ast.walk(child):
                        if isinstance(sub, ast.Call):
                            f = sub.func
                            if isinstance(f, ast.Name):
                                calls.add(f.id)
                            elif isinstance(f, ast.Attribute):
                                calls.add(f.attr)
                    syms.append(
                        Symbol(
                            name=name,
                            path=rel,
                            line=child.lineno,
                            end_line=getattr(child, "end_lineno", child.lineno)
                            or child.lineno,
                            kind=kind,
                            calls=calls,
                        )
                    )
                    visit(child, name + ".")
                else:
                    visit(child, prefix)

        visit(tree, "")
        syms.sort(key=lambda s: s.line)
        return syms

    # queries ---------------------------------------------------------------
    def enclosing(self, path: str, line: int) -> Symbol | None:
        """Return the innermost symbol of ``path`` that spans ``line``, or ``None``."""
        best: Symbol | None = None
        for s in self.symbols.get(path, []):
            if s.line <= line <= s.end_line and (best is None or s.line >= best.line):
                best = s
        return best

    def changed_symbols(self, files: list[ChangedFile]) -> list[Symbol]:
        """Return each symbol that encloses a changed line, once.

        Deleted and binary files are skipped.
        """
        out: dict[tuple[str, str], Symbol] = {}
        for f in files:
            if f.status in ("deleted", "binary"):
                continue
            for ln in f.changed_lines():
                s = self.enclosing(f.path, ln)
                if s is not None:
                    out[(f.path, s.name)] = s
        return list(out.values())

    def callers(self, name: str) -> list[Symbol]:
        """Return the symbols whose code calls ``name``, by word-boundary search.

        Only the last dotted part of ``name`` is searched for, and a name shorter
        than three characters gives ``[]``. Each calling symbol is listed once;
        a call outside any symbol is listed as a ``<module>`` symbol at that line.
        """
        short = name.split(".")[-1]
        if len(short) < 3:
            return []
        pat = re.compile(rf"\b{re.escape(short)}\s*\(")
        defs = {(s.path, s.line) for s in self.by_name.get(short, [])}
        found: dict[tuple[str, str], Symbol] = {}
        for path, text in self.files.items():
            for m in pat.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                if (path, line) in defs:
                    continue
                enc = self.enclosing(path, line)
                if enc is None:
                    enc = Symbol(
                        name="<module>",
                        path=path,
                        line=line,
                        end_line=line,
                        kind="module",
                    )
                found.setdefault((path, enc.name), enc)
        return list(found.values())

    def callees(self, sym: Symbol) -> list[Symbol]:
        """Return the definitions of the names ``sym`` calls, at most three per name."""
        out: list[Symbol] = []
        for c in sorted(sym.calls):
            out.extend(self.by_name.get(c, [])[:3])
        return out

    def blast_radius(self, files: list[ChangedFile], depth: int = 2) -> GraphReport:
        """Follow callers outwards from the symbols a change touches.

        Args:
            files: The changed files.
            depth: How many levels of callers to follow.

        Returns:
            The changed symbols, up to 25 callers of each symbol visited, every
            file that is changed or holds a caller, and a one-line summary.
        """
        changed = self.changed_symbols(files)
        callers: dict[str, list[SymbolRef]] = {}
        impacted: set[str] = {f.path for f in files}
        frontier = [(s, 0) for s in changed]
        seen: set[tuple[str, str]] = set()
        while frontier:
            sym, d = frontier.pop()
            if d >= depth or (sym.path, sym.name) in seen:
                continue
            seen.add((sym.path, sym.name))
            cs = self.callers(sym.name)
            if cs:
                callers[f"{sym.path}:{sym.name}"] = [c.ref() for c in cs[:25]]
            for c in cs:
                impacted.add(c.path)
                frontier.append((c, d + 1))
        n_callers = sum(len(v) for v in callers.values())
        summary = (
            f"{len(changed)} changed symbol(s) in {len(files)} file(s); "
            f"{n_callers} caller site(s) within depth {depth}; "
            f"{len(impacted)} file(s) potentially impacted."
        )
        return GraphReport(
            changed_symbols=[s.ref() for s in changed],
            callers=callers,
            impacted_files=sorted(impacted),
            summary=summary,
        )

    def query(self, name: str) -> dict[str, Any]:
        """Describe ``name`` for the agents' ``code_graph_query`` tool.

        Returns:
            Up to 10 definitions, 30 callers, and 30 callees of the first
            definition, each as a ``SymbolRef`` dict.
        """
        short = name.split(".")[-1]
        defs = self.by_name.get(short, [])
        return {
            "symbol": name,
            "definitions": [d.ref().model_dump() for d in defs[:10]],
            "callers": [c.ref().model_dump() for c in self.callers(short)[:30]],
            "callees": [
                c.ref().model_dump() for d in defs[:1] for c in self.callees(d)[:30]
            ],
        }


def _brace_end(lines: list[str], start_idx: int, max_span: int = 2000) -> int:
    depth = 0
    opened = False
    for i in range(start_idx, min(len(lines), start_idx + max_span)):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
                if opened and depth == 0:
                    return i + 1
    return min(len(lines), start_idx + 1)
