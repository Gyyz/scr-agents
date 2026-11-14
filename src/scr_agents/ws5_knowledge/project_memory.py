"""Project memory, stored inside the reviewed repository under `.scr_agents/`.

    .scr_agents/memory.md
        notes for the agents: conventions, known false positives, how to build and
        test. Humans edit it; the lead agent appends when asked to remember.
    .scr_agents/project.yaml
        overrides: bench build/test commands, ignored paths and scanner rules.
    .scr_agents/findings.jsonl
        one line per finding per run, updated with human verdicts; findings are
        linked across runs by a fingerprint (rule, else code quote, else title).
    .scr_agents/state.json
        summaries of the last runs.

Commit the directory to share the memory with the team. Everything read from it is
treated as untrusted text when it reaches a prompt (it is part of the repository after
all).

Trust boundary: the memory suppresses findings and overrides build commands, so the
change under review must not be able to rewrite it. When the reviewed range touches
`.scr_agents/`, the pipeline constructs the memory with `read_ref=<base commit>` and
every read comes from that commit via `git show`; otherwise the working tree is read.
Symlinks in or at `.scr_agents/` are never followed.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scr_agents.inputs import git as g
from scr_agents.models import EvidenceReport, Finding, now_iso

MEMORY_DIR = ".scr_agents"
# the folder name before the project was renamed: a repository that has it (and not
# the new one) keeps using it, so its notes and findings history carry over
LEGACY_MEMORY_DIR = ".codecheck"
MEMORY_DIRS = (MEMORY_DIR, LEGACY_MEMORY_DIR)
MEMORY_HEADER = (
    "# scr_agents project memory\n"
    "\n"
    "Notes read by the review agents before every review of this repository. "
    "Keep entries short and\n"
    "factual. Sections are free-form; the agents see the whole file.\n"
    "\n"
    "## Build and test\n"
    "\n"
    "## Conventions\n"
    "\n"
    "## Known false positives\n"
    "\n"
    "## Notes\n"
)


class BenchOverride(BaseModel):
    """The ``bench`` section: argv lists that replace the configured build and test."""

    model_config = ConfigDict(extra="forbid")
    build_cmd: list[str] | None = None
    test_cmd: list[str] | None = None


class IgnoreOverride(BaseModel):
    """The ``ignore`` section: paths and scanner rules whose findings are dropped."""

    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(default_factory=list)  # fnmatch globs
    rules: list[str] = Field(
        default_factory=list
    )  # scanner rule ids, with or without the scanner prefix


class ProjectOverrides(BaseModel):
    """Schema of `.scr_agents/project.yaml`.

    Anything that does not validate is reported and ignored.
    """

    model_config = ConfigDict(extra="forbid")
    bench: BenchOverride = Field(default_factory=BenchOverride)
    ignore: IgnoreOverride = Field(default_factory=IgnoreOverride)


def fingerprint(
    path: str,
    rule: str | None,
    title: str,
    *,
    category: str = "",
    quote: str | None = None,
) -> str:
    """Stable identity of a finding across runs.

    Built from the path and the scanner rule when there is one, else the quoted code
    line plus category, else the title's words. Model-written titles change between
    runs; the code line does not.
    """
    if rule:
        key = "rule:" + rule.strip().lower()
    elif quote and quote.strip():
        key = f"quote:{category}:{' '.join(quote.split()).lower()}"
    else:
        key = "title:" + re.sub(r"[\d\W]+", " ", title.lower()).strip()
    return hashlib.sha1(f"{path}\0{key}".encode()).hexdigest()[:12]


def finding_fingerprint(f: Finding) -> str:
    """Return the ``fingerprint`` of a finding, from its own fields."""
    return fingerprint(
        f.location.path,
        f.scanner_rule,
        f.title,
        category=f.category.value,
        quote=f.code_quote,
    )


class ProjectMemory:
    """The project memory of one repository: notes, overrides and findings history."""

    def __init__(self, repo: str | Path, read_ref: str | None = None):
        """Pick the folder: ``.scr_agents``, or ``.codecheck`` when only that exists.

        Args:
            repo: The reviewed repository.
            read_ref: Commit to read the memory from instead of the working tree
                (see the module docstring). Writes always go to the working tree.
        """
        self.repo = Path(repo)
        # commit to read from instead of the working tree (see module docstring)
        self.read_ref = read_ref
        self.name = self._pick_name()
        self.dir = self.repo / self.name
        self.errors: list[str] = []
        self._overrides: ProjectOverrides | None = None

    # ------------------------------------------------------------------ storage
    def _has(self, name: str) -> bool:
        if self.read_ref:
            return bool(
                g.git(
                    self.repo, "ls-tree", "-d", self.read_ref, "--", name, check=False
                ).strip()
            )
        p = self.repo / name
        return p.is_dir() or p.is_symlink()

    def _pick_name(self) -> str:
        """``.scr_agents``, unless only the legacy ``.codecheck`` folder exists."""
        return (
            LEGACY_MEMORY_DIR
            if not self._has(MEMORY_DIR) and self._has(LEGACY_MEMORY_DIR)
            else MEMORY_DIR
        )

    @property
    def exists(self) -> bool:
        """Whether the memory folder exists; a symlink in its place does not count."""
        if self.read_ref:
            return self._has(self.name)
        return self.dir.is_dir() and not self.dir.is_symlink()

    @property
    def read_from(self) -> str:
        """Where reads come from: ``"worktree"``, or ``"base (...)"`` with a commit."""
        return f"base ({self.read_ref[:12]})" if self.read_ref else "worktree"

    def _err(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)

    def _wt_path(self, name: str) -> Path | None:
        """Working-tree path of a memory file.

        Returns None when the directory or the file is a symlink.
        """
        if self.dir.is_symlink():
            self._err(f"{self.name} is a symlink; refusing to read or write it")
            return None
        p = self.dir / name
        if p.is_symlink():
            self._err(f"{self.name}/{name} is a symlink; ignored")
            return None
        return p

    def _read(self, name: str) -> str | None:
        if self.read_ref:
            try:
                return g.git(self.repo, "show", f"{self.read_ref}:{self.name}/{name}")
            except g.GitError:
                return None
        p = self._wt_path(name)
        if p is None or not p.is_file():
            return None
        return p.read_text(encoding="utf-8", errors="replace")

    def _write_path(self, name: str) -> Path:
        """Working-tree path to write, creating the memory folder if needed.

        Raises OSError rather than writing through a symlink.
        """
        if self.dir.is_symlink():
            raise OSError(
                f"{self.dir} is a symlink; refusing to write project memory through it"
            )
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self._wt_path(name)
        if p is None:
            raise OSError(
                f"{self.dir / name} is a symlink; refusing to write through it"
            )
        return p

    @staticmethod
    def _replace(p: Path, text: str) -> None:
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)

    # ------------------------------------------------------------------ notes
    def notes(self) -> str:
        """Return the text of ``memory.md``, or ``""`` if there is none."""
        return self._read("memory.md") or ""

    def append_note(
        self, text: str, section: str = "Notes", author: str = "lead"
    ) -> Path:
        """Add a dated bullet to a section of ``memory.md`` in the working tree.

        A missing file starts from the standard header, and a missing section is
        added at the end.

        Args:
            text: The note.
            section: Heading to add it under, without the ``##``.
            author: Who wrote the note, shown in the entry.

        Returns:
            The path of ``memory.md``.

        Raises:
            OSError: If the memory folder or ``memory.md`` is a symlink, or the
                file cannot be written.
        """
        p = self._write_path("memory.md")
        content = (
            p.read_text(encoding="utf-8", errors="replace")
            if p.is_file()
            else MEMORY_HEADER
        )
        entry = f"- {now_iso()[:10]} ({author}) {text.strip()}\n"
        heading = f"## {section.strip()}"
        if heading in content:
            head, _, tail = content.partition(heading)
            # insert at the end of the section (before the next heading)
            m = re.search(r"\n## ", tail)
            if m:
                tail = tail[: m.start()].rstrip("\n") + "\n" + entry + tail[m.start() :]
            else:
                tail = tail.rstrip("\n") + "\n" + entry
            content = head + heading + tail
        else:
            content = content.rstrip("\n") + f"\n\n{heading}\n\n{entry}"
        self._replace(p, content)
        return p

    # ------------------------------------------------------------------ overrides
    def overrides(self) -> ProjectOverrides:
        """Return the ``project.yaml`` overrides, parsed once and then cached.

        Invalid YAML or values are recorded in ``errors`` and the defaults are
        used instead.
        """
        if self._overrides is None:
            data: Any = {}
            raw = self._read("project.yaml")
            if raw:
                try:
                    data = yaml.safe_load(raw) or {}
                except yaml.YAMLError as e:
                    self._err(f"project.yaml: invalid YAML: {str(e).splitlines()[0]}")
                    data = {}
                if not isinstance(data, dict):
                    self._err("project.yaml: top level must be a mapping")
                    data = {}
            try:
                self._overrides = ProjectOverrides.model_validate(data)
            except ValidationError as e:
                problems = "; ".join(
                    f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                    for err in e.errors()[:5]
                )
                self._err(
                    f"project.yaml ignored, {e.error_count()} invalid value(s): "
                    f"{problems}"
                )
                self._overrides = ProjectOverrides()
        return self._overrides

    def is_ignored(self, path: str, rule: str | None = None) -> bool:
        """Return whether ``project.yaml`` drops findings at ``path`` or from ``rule``.

        Paths are matched as fnmatch globs. A listed rule id matches with or
        without the scanner prefix, so ``E501`` also matches ``ruff:E501``.
        """
        ign = self.overrides().ignore
        for pat in ign.paths:
            if fnmatch.fnmatch(path, str(pat)):
                return True
        if rule:
            rules = {str(r).lower() for r in ign.rules}
            if rule.lower() in rules or rule.split(":")[-1].lower() in rules:
                return True
        return False

    # -------------------------------------------------------------- findings history
    def history(self) -> list[dict[str, Any]]:
        """Return the records of ``findings.jsonl``, oldest first.

        Lines that are not JSON objects with a fingerprint are skipped.
        """
        raw = self._read("findings.jsonl")
        if not raw:
            return []
        out = []
        for line in raw.splitlines():
            if line.strip():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("fingerprint"):
                    out.append(rec)
        return out

    def prior_for(self, finding: Finding, limit: int = 5) -> list[dict[str, Any]]:
        """Return the last ``limit`` history records with ``finding``'s fingerprint."""
        fp = finding_fingerprint(finding)
        hits = [h for h in self.history() if h.get("fingerprint") == fp]
        return hits[-limit:]

    def record_run(self, report: EvidenceReport, session_id: str | None = None) -> int:
        """Add a finished run to the findings history and to ``state.json``.

        Each finding becomes one line of ``findings.jsonl``. ``state.json`` keeps
        a summary of the last 20 runs; a run recorded again replaces its entry.

        Args:
            report: The finished run.
            session_id: Session the run belongs to, stored with each record.

        Returns:
            The number of findings recorded.

        Raises:
            OSError: If the memory folder or a file in it is a symlink, or a file
                cannot be written.
        """
        lines = []
        for vf in report.findings:
            f = vf.finding
            lines.append(
                json.dumps(
                    {
                        "ts": report.created_at,
                        "run_id": report.run_id,
                        "session_id": session_id,
                        "finding_id": f.id,
                        "fingerprint": finding_fingerprint(f),
                        "path": f.location.path,
                        "line": f.location.line,
                        "rule": f.scanner_rule,
                        "title": f.title,
                        "category": f.category.value,
                        "severity": f.severity.value,
                        "source": f.source,
                        "verdict": vf.verdict.value,
                        "human_verdict": None,
                        "human_note": None,
                        "range": (
                            f"{report.request.base[:10]}..{report.request.head[:10]}"
                        ),
                    },
                    default=str,
                )
            )
        findings_path = self._write_path("findings.jsonl")
        with findings_path.open("a", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in lines))
        state = self._state()
        runs = [r for r in state.get("runs", []) if r.get("run_id") != report.run_id]
        runs.append(
            {
                "run_id": report.run_id,
                "ts": report.created_at,
                "session_id": session_id,
                "range": f"{report.request.base[:10]}..{report.request.head[:10]}",
                "scope": report.request.scope,
                "counts": report.counts(),
                "bench": report.bench.delta if report.bench else None,
                "test_cmd": (
                    report.bench.head.command
                    if report.bench
                    and report.bench.head
                    and not report.bench.head.skipped
                    else None
                ),
            }
        )
        state["runs"] = runs[-20:]
        self._replace(
            self._write_path("state.json"),
            json.dumps(state, indent=2, default=str) + "\n",
        )
        return len(lines)

    def record_human_verdict(
        self, run_id: str, finding_id: str, verdict: str, note: str = ""
    ) -> bool:
        """Store a human verdict on the finding's records in ``findings.jsonl``.

        Only the working tree is updated. ``note`` and the time are stored with
        the verdict.

        Returns:
            Whether a record matched ``run_id`` and ``finding_id``; ``False`` also
            when there is no findings history.
        """
        p = self._wt_path("findings.jsonl")
        if p is None or not p.is_file():
            return False
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        changed = False
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("run_id") == run_id and rec.get("finding_id") == finding_id:
                rec["human_verdict"], rec["human_note"], rec["human_ts"] = (
                    verdict,
                    note,
                    now_iso(),
                )
                lines[i] = json.dumps(rec, default=str)
                changed = True
        if changed:
            self._replace(p, "\n".join(lines) + "\n")
        return changed

    def _state(self) -> dict[str, Any]:
        raw = self._read("state.json")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    # ---------------------------------------------------------- for prompts and tools
    def summary(self, notes_chars: int = 6000) -> dict[str, Any]:
        """Summarise the memory for the agents' prompts and tools and for the console.

        Args:
            notes_chars: How much of ``memory.md`` to include.

        Returns:
            A JSON-ready dict: where the memory is and where it was read from, the
            notes, the overrides set, load errors, the last 10 runs, how many
            findings were recorded, the last 20 findings humans rejected and up
            to 10 findings recorded more than once.
        """
        hist = self.history()
        rejected = [h for h in hist if h.get("human_verdict") == "reject"]
        recurring: dict[str, int] = {}
        for h in hist:
            recurring[h["fingerprint"]] = recurring.get(h["fingerprint"], 0) + 1
        top = sorted(recurring.items(), key=lambda kv: -kv[1])[:10]
        by_fp = {h["fingerprint"]: h for h in hist}
        return {
            "dir": str(self.dir),
            "exists": self.exists,
            "read_from": self.read_from,
            "notes": self.notes()[:notes_chars],
            "overrides": self.overrides().model_dump(exclude_defaults=True),
            "errors": list(self.errors),
            "runs": self._state().get("runs", [])[-10:],
            "findings_recorded": len(hist),
            "human_rejected": [
                {
                    "path": h.get("path"),
                    "rule": h.get("rule"),
                    "title": h.get("title"),
                    "note": h.get("human_note"),
                }
                for h in rejected[-20:]
            ],
            "recurring": [
                {
                    "path": by_fp[fp].get("path"),
                    "rule": by_fp[fp].get("rule"),
                    "title": by_fp[fp].get("title"),
                    "times": n,
                }
                for fp, n in top
                if n > 1
            ],
        }

    def prior_lines(self, finding: Finding) -> list[str]:
        """Describe the earlier verdicts on ``finding``, one line per history record.

        Covers the records ``prior_for`` returns. Each line gives the date, the run,
        the agent's verdict and, when there is one, the human verdict with its note.
        """
        out = []
        for h in self.prior_for(finding):
            line = f"{h.get('ts', '')[:10]} {h.get('run_id')}: agent {h.get('verdict')}"
            if h.get("human_verdict"):
                line += f", human {h['human_verdict']}" + (
                    f" ({h['human_note']})" if h.get("human_note") else ""
                )
            out.append(line)
        return out
