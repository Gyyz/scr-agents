"""Shared data model: the review request and everything derived from it.

The review request is the shared input format every workstream reads.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string, to the second."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """Return ``prefix`` joined to eight random hex digits, e.g. ``run-1a2b3c4d``."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------- inputs


class Task(StrEnum):
    """Review goal: find defects, or check that a change fixes what ``claim`` says."""

    FIND_BUGS = "find_bugs"
    VERIFY_FIX = "verify_fix"


def split_lines(text: str) -> list[str]:
    """Split on newlines only.

    ``str.splitlines()`` also splits on form feeds and Unicode separators, which
    shifts every line number after them.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


class Hunk(BaseModel):
    """One ``@@`` hunk of a unified diff: its line ranges, header and body."""

    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    header: str = ""
    text: str = ""  # hunk body; every line starts with ' ', '+', '-' or '\\'

    def added_line_numbers(self) -> list[int]:
        """Line numbers (in the new file) of lines added or modified by this hunk."""
        nums: list[int] = []
        n = self.new_start
        for line in split_lines(self.text):
            if line.startswith("+"):
                nums.append(n)
                n += 1
            elif line.startswith(("-", "\\")):
                continue
            else:
                n += 1
        return nums


class ChangedFile(BaseModel):
    """A file under review and its diff hunks.

    In whole-repository scope (``scope="all"``) every code file appears as added.
    """

    path: str
    old_path: str | None = None
    status: Literal["added", "modified", "deleted", "renamed", "binary"] = "modified"
    language: str | None = None
    hunks: list[Hunk] = Field(default_factory=list)
    added: int = 0
    removed: int = 0

    def changed_lines(self) -> list[int]:
        """Return the new-file line numbers added or modified across all hunks."""
        out: list[int] = []
        for h in self.hunks:
            out.extend(h.added_line_numbers())
        return out

    def diff_text(self) -> str:
        """Render the hunks back as a unified diff with ``a/`` and ``b/`` headers."""
        parts = [f"--- a/{self.old_path or self.path}", f"+++ b/{self.path}"]
        for h in self.hunks:
            head = (
                f"@@ -{h.old_start},{h.old_lines} "
                f"+{h.new_start},{h.new_lines} @@ {h.header}"
            )
            parts.append(head.rstrip())
            parts.append(h.text.rstrip("\n"))
        return "\n".join(parts) + "\n"


class Commit(BaseModel):
    """A commit in the reviewed range."""

    sha: str
    author: str = ""
    date: str = ""
    subject: str = ""
    body: str = ""


class PRComment(BaseModel):
    """A pull request comment; ``path`` and ``line`` are set on inline comments."""

    author: str = ""
    body: str = ""
    path: str | None = None
    line: int | None = None


class PullRequest(BaseModel):
    """Pull request metadata and comments, when the review starts from a PR."""

    number: int | None = None
    title: str = ""
    body: str = ""
    author: str = ""
    url: str | None = None
    comments: list[PRComment] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    """The shared input format every workstream reads."""

    run_id: str = Field(default_factory=lambda: new_id("run"))
    session_id: str | None = None  # out/sessions/<session_id>/<run_id>/
    repo_path: str
    base_ref: str
    head_ref: str
    base: str  # resolved sha
    head: str  # resolved sha
    task: Task = Task.FIND_BUGS
    claim: str | None = None  # for verify_fix: what the change claims to fix
    instructions: str | None = (
        None  # what the user asked for, in their words (interactive sessions)
    )
    scope: Literal["range", "all"] = (
        "range"  # all = whole repository at head, not a diff
    )
    files: list[ChangedFile] = Field(default_factory=list)
    commits: list[Commit] = Field(default_factory=list)
    pr: PullRequest | None = None
    created_at: str = Field(default_factory=now_iso)

    @property
    def commit_ids(self) -> list[str]:
        """Return the range's commit SHAs, with base and head added if missing."""
        ids = [c.sha for c in self.commits]
        for sha in (self.base, self.head):
            if sha not in ids:
                ids.append(sha)
        return ids

    def file(self, path: str) -> ChangedFile | None:
        """Return the entry for ``path``, or ``None`` if the request lacks it."""
        return next((f for f in self.files if f.path == path), None)

    def diff_text(self, max_chars: int | None = None) -> str:
        """Concatenate the unified diffs of all non-binary files.

        Args:
            max_chars: Cut the text to this many characters and append a
                truncation marker; ``None`` keeps it whole.
        """
        text = "".join(f.diff_text() for f in self.files if f.status != "binary")
        if max_chars is not None and len(text) > max_chars:
            text = text[:max_chars] + f"\n... [diff truncated at {max_chars} chars]\n"
        return text


# -------------------------------------------------------------------------- WS2 outputs


class ScannerFinding(BaseModel):
    """One raw hit from a static scanner or builtin rule, before any agent sees it."""

    scanner: str
    rule: str
    message: str
    path: str
    line: int | None = None
    severity: str = "medium"
    category: str = "bug"  # bug | vulnerability | style
    cwe: list[str] = Field(default_factory=list)


class ScannerReport(BaseModel):
    """Scanner hits for a run, and which scanners ran, were missing or failed."""

    findings: list[ScannerFinding] = Field(default_factory=list)
    scanners_run: list[str] = Field(default_factory=list)
    scanners_missing: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class SymbolRef(BaseModel):
    """A symbol definition in the code graph: name, file, line and kind."""

    name: str
    path: str
    line: int
    kind: str = "function"


class GraphReport(BaseModel):
    """Code graph impact of the change: changed symbols, their callers, impacted files.

    ``callers`` is keyed by ``path:symbol`` of the changed symbol.
    """

    changed_symbols: list[SymbolRef] = Field(default_factory=list)
    callers: dict[str, list[SymbolRef]] = Field(default_factory=dict)
    impacted_files: list[str] = Field(default_factory=list)
    summary: str = ""


# -------------------------------------------------------------------------- WS1 outputs


class Severity(StrEnum):
    """Finding severity, most severe first."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Category(StrEnum):
    """What kind of problem a finding describes."""

    BUG = "bug"
    VULNERABILITY = "vulnerability"
    STYLE = "style"
    NAMING = "naming"
    PERFORMANCE = "performance"
    OTHER = "other"


class Location(BaseModel):
    """Where a finding points: file, optional line range and enclosing symbol."""

    path: str
    line: int | None = None
    end_line: int | None = None
    symbol: str | None = None


class Finding(BaseModel):
    """A suspected problem from a subagent or scanner, before validation."""

    id: str = Field(default_factory=lambda: new_id("F"))
    title: str
    category: Category = Category.BUG
    severity: Severity = Severity.MEDIUM
    location: Location
    description: str = ""
    rationale: str = ""
    cwe: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    suggested_fix: str | None = None
    reproduction: str | None = None
    source: str = "subagent"
    confidence: float = 0.5
    scanner_rule: str | None = None
    code_quote: str | None = (
        None  # the line at `location` in the head checkout; anchors the fingerprint
    )
    merged_from: list[str] = Field(
        default_factory=list
    )  # sources merged into this finding (subagents, scanners)
    agreement: int = 1  # how many independent sources reported it
    grounded: bool = (
        True  # path exists, line is inside the file and (range mode) near the change
    )


class Verdict(StrEnum):
    """The validator's decision on a finding."""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNVERIFIED = "unverified"


class Evidence(BaseModel):
    """One piece of evidence behind a verdict, such as a test run or command output."""

    kind: Literal["test_run", "build", "command", "scanner", "reasoning"] = "reasoning"
    summary: str = ""
    command: str | None = None
    output: str | None = None
    exit_code: int | None = None
    ref: str | None = None


class ValidatedFinding(BaseModel):
    """A finding with the validator's verdict, its evidence and an optional fix."""

    finding: Finding
    verdict: Verdict = Verdict.UNVERIFIED
    validator_notes: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    fix: str | None = None
    prior: list[dict[str, Any]] = Field(
        default_factory=list
    )  # earlier runs of the same finding (project memory)
    # what backs the verdict: differential (base and head behave differently when
    # run), executed (something was built or run), traced (lines cited, nothing
    # run), none
    evidence_strength: str = "none"
    round: int = 1  # the review round that reported it
    # set on a finding settled in an earlier round and carried into this report: that
    # round's run, and the human verdict that settled it (accept or reject)
    carried_from: str | None = None
    human_verdict: str | None = None
    human_note: str = ""


# -------------------------------------------------------------------------- WS3 outputs


class BuildResult(BaseModel):
    """Outcome of building or testing one checkout (base or head) in the sandbox."""

    ref: str
    command: str = ""
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    passed: bool = False
    skipped: bool = False
    reason: str = ""


class BenchReport(BaseModel):
    """Base and head build or test results, and a one-line ``delta`` between them."""

    base: BuildResult | None = None
    head: BuildResult | None = None
    delta: str = ""


class ReviewPlan(BaseModel):
    """The planner's output: files to focus on and hypotheses for the subagents."""

    focus_files: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    notes: str = ""


class EvidenceReport(BaseModel):
    """The final review result: validated findings plus everything gathered for them."""

    run_id: str
    request: ReviewRequest
    plan: ReviewPlan = Field(default_factory=ReviewPlan)
    findings: list[ValidatedFinding] = Field(default_factory=list)
    scanner: ScannerReport = Field(default_factory=ScannerReport)
    graph: GraphReport = Field(default_factory=GraphReport)
    bench: BenchReport | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(
        default_factory=list
    )  # agents that failed; the review is incomplete if non-empty
    created_at: str = Field(default_factory=now_iso)
    # review rounds: this report is round ``round`` of at most ``max_rounds``; a later
    # round names the run before it and the ids of that run's findings it re-reviewed
    round: int = 1
    max_rounds: int = 1
    previous_run_id: str | None = None
    replaced: list[str] = Field(default_factory=list)
    auto_rounds: bool = False  # settled by the validator alone, no human verdicts

    @property
    def incomplete(self) -> bool:
        """Return ``True`` when an agent failed, so findings may be missing."""
        return bool(self.errors)

    def counts(self) -> dict[str, int]:
        """Return the number of findings per verdict, including verdicts with none."""
        out = {v.value: 0 for v in Verdict}
        for f in self.findings:
            out[f.verdict.value] += 1
        return out


class RoundStatus(BaseModel):
    """Where a review round stands, from its report and the human verdicts so far.

    A finding is settled when the person reviewing agrees with the validator (or, in
    an automatic run, when the validator confirmed or rejected it); every other
    finding is pending and is re-reviewed if another round starts.
    """

    run_id: str
    round: int = 1
    max_rounds: int = 1
    # the review report of this round: Review Report, then Review Report (round 2), ...
    label_a: str = "Review Report"
    # the validator's comment report on it: Validator Report, then ... (round 2), ...
    label_b: str = "Validator Report"
    auto: bool = False
    # every finding of this round is confirmed by the validator
    validator_passed: bool = True
    settled_real: list[str] = Field(default_factory=list)
    settled_false: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)
    disputed: list[str] = Field(
        default_factory=list
    )  # pending because the human verdict contradicts the validator
    awaiting_verdict: list[str] = Field(
        default_factory=list
    )  # pending because no human verdict was given yet
    passed: bool = False  # nothing is pending
    can_continue: bool = False  # something is pending and the round limit allows more
    ask_user: bool = False  # interactive run that should offer the next round
