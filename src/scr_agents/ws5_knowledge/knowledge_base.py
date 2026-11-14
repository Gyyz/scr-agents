"""Knowledge base: YAML entries for CWE, CERT C and MISRA rules with keyword lookup."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from scr_agents.config import KNOWLEDGE_DIR


@dataclass
class KBEntry:
    """One rule or weakness (a CWE, CERT C or MISRA entry) as loaded from YAML."""

    id: str
    standard: str
    title: str
    summary: str = ""
    languages: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    mitigations: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the entry as a plain dict, ready for JSON."""
        return asdict(self)


_WORD = re.compile(r"[A-Za-z0-9_\-]+")


class KnowledgeBase:
    """In-memory store of ``KBEntry`` records, keyed by upper-cased id."""

    def __init__(self, dirs: list[Path] | None = None):
        """Load every ``*.yaml`` and ``*.yml`` file in the bundled knowledge directory.

        Args:
            dirs: Extra directories to load after the bundled one. Missing
                directories are skipped; an entry whose id was already loaded
                replaces the earlier one.
        """
        self.entries: dict[str, KBEntry] = {}
        for d in [KNOWLEDGE_DIR, *(dirs or [])]:
            self._load_dir(Path(d))

    def _load_dir(self, d: Path) -> None:
        if not d.exists():
            return
        for f in sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml")):
            data = yaml.safe_load(f.read_text()) or {}
            for e in data.get("entries", []):
                kwargs: dict[str, Any] = {}
                for k in KBEntry.__dataclass_fields__:
                    v = e.get(k)
                    if k in ("languages", "keywords", "mitigations", "references"):
                        kwargs[k] = [str(x) for x in (v or []) if x is not None]
                    else:
                        kwargs[k] = "" if v is None else str(v)
                entry = KBEntry(**kwargs)
                self.entries[entry.id.upper()] = entry

    def get(self, entry_id: str) -> KBEntry | None:
        """Return the entry with ``entry_id`` (any case), or ``None`` if unknown."""
        return self.entries.get(entry_id.upper())

    def lookup(
        self, query: str, limit: int = 5, language: str | None = None
    ) -> list[KBEntry]:
        """Rank entries against ``query`` by keyword overlap.

        An exact id match weighs most, then shared keywords, title words and
        summary words, in that order.

        Args:
            query: Free text or an entry id such as ``"CWE-78"``.
            limit: Maximum number of entries to return.
            language: When given, entries limited to other languages score
                half.

        Returns:
            Up to ``limit`` entries, best match first; entries with no overlap
            are left out.
        """
        words = {w.lower() for w in _WORD.findall(query)}
        scored: list[tuple[float, KBEntry]] = []
        for e in self.entries.values():
            score = 0.0
            if e.id.lower() in words or query.strip().upper() == e.id:
                score += 10
            hay_kw = {k.lower() for k in e.keywords}
            score += 3 * len(words & hay_kw)
            title_words = {w.lower() for w in _WORD.findall(e.title)}
            score += 2 * len(words & title_words)
            summary_words = {w.lower() for w in _WORD.findall(e.summary)}
            score += 0.5 * len(words & summary_words)
            if language and e.languages and language not in e.languages:
                score *= 0.5
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda t: (-t[0], t[1].id))
        return [e for _, e in scored[:limit]]
