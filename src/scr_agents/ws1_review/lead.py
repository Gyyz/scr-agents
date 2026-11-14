"""Lead agent for interactive sessions.

It turns a person's request into reviews (the whole pipeline is one of its tools) and
answers questions about the code and the findings.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Protocol

from scr_agents.inputs import git as g
from scr_agents.llm.backend import LLMBackend, ToolSpec
from scr_agents.llm.loop import LoopResult, ToolLoop
from scr_agents.models import EvidenceReport, split_lines
from scr_agents.ws1_review.rounds import round_status
from scr_agents.ws4_guardrails.registry import PromptRegistry
from scr_agents.ws4_guardrails.safety_filter import SafetyFilter
from scr_agents.ws5_knowledge.activity_log import ActivityLog
from scr_agents.ws5_knowledge.human_review import load_verdicts, record_verdict

_UNTRUSTED = re.compile(r"<untrusted[^>]*>\n?([\s\S]*?)\n?</untrusted>")


class SessionLike(Protocol):
    """What the lead agent needs from the interactive session."""

    @property
    def repo(self) -> Path | None:
        """Return the repository folder; None while a web chat is still fetching it."""

    base: str
    head: str
    scope: str
    last_report: EvidenceReport | None
    last_run_dir: Path | None
    memory: Any
    interface: str  # "terminal" or "web console": how the person opens a finding

    @property
    def skills(self) -> Any:  # SkillLibrary or None
        """Return the skills the lead agent may read."""

    def run_review(
        self,
        base: str | None = None,
        head: str | None = None,
        task: str = "find_bugs",
        claim: str | None = None,
        instructions: str | None = None,
        scope: str | None = None,
        orchestrator: str | None = None,
    ) -> EvidenceReport:
        """Run the full review pipeline and keep its report as ``last_report``.

        Arguments left as ``None`` fall back to the session's own range and scope,
        and ``orchestrator`` to ``review.orchestrator``.
        """
        ...

    def run_next_round(self) -> EvidenceReport:
        """Run the next review round of the last review and keep it as ``last_report``.

        Raises:
            RoundError: If the last review passed or reached the round limit.
        """
        ...

    def project_info(self) -> dict[str, Any]:
        """Return repository facts and the current review scope."""
        ...


def summarize_report(
    report: EvidenceReport, run_dir: Path | None, limit: int = 15
) -> dict[str, Any]:
    """Condense a review report into the summary the lead agent and its tools return.

    Findings are listed confirmed first, then unverified, then rejected.

    Args:
        report: The review report.
        run_dir: The run directory holding ``report.md``, if known.
        limit: How many findings to list; ``more_findings`` counts the rest.
    """
    order = {"confirmed": 0, "unverified": 1, "rejected": 2}
    findings = sorted(report.findings, key=lambda v: order.get(v.verdict.value, 9))
    verdicts = load_verdicts(run_dir) if run_dir else []
    human = {str(v.get("finding_id")): v.get("verdict") for v in verdicts}
    st = round_status(report, verdicts)
    return {
        "run_id": report.run_id,
        "counts": report.counts(),
        "scope": report.request.scope,
        "range": f"{report.request.base[:10]}..{report.request.head[:10]}",
        "test_bench": report.bench.delta if report.bench else None,
        "report_path": str(run_dir / "report.md") if run_dir else None,
        "round": {
            **st.model_dump(
                include={
                    "round",
                    "max_rounds",
                    "validator_passed",
                    "pending",
                    "disputed",
                    "awaiting_verdict",
                    "passed",
                    "can_continue",
                    "ask_user",
                }
            ),
            "review_report": {
                "name": st.label_a,
                "path": str(run_dir / "report_a.html") if run_dir else None,
            },
            "validator_report": {
                "name": st.label_b,
                "path": str(run_dir / "report_b.html") if run_dir else None,
            },
        },
        "findings": [
            {
                "id": v.finding.id,
                "verdict": v.verdict.value,
                "severity": v.finding.severity.value,
                "category": v.finding.category.value,
                "title": v.finding.title,
                "location": f"{v.finding.location.path}:"
                f"{v.finding.location.line or '?'}",
                "cwe": v.finding.cwe,
                "human_verdict": human.get(v.finding.id) or v.human_verdict,
                "carried_from": v.carried_from,
            }
            for v in findings[:limit]
        ],
        "more_findings": max(0, len(findings) - limit),
    }


def lead_tools(session: SessionLike, safety: SafetyFilter) -> list[ToolSpec]:
    """Build the lead agent's tools over the session.

    They start a review, show the findings of the last one, read and search the
    repository, read the operators' skills, and read and add to the project memory.
    """

    def _obj(
        props: dict[str, Any], required: list[str] | None = None
    ) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": props,
            "required": required or [],
            "additionalProperties": False,
        }

    def _root() -> Path:
        """Return the repository folder; tools cannot run before it exists."""
        if session.repo is None:
            raise ValueError("the repository is not ready yet")
        return session.repo

    def start_review(inp: dict[str, Any]) -> dict[str, Any]:
        report = session.run_review(
            base=inp.get("base") or None,
            head=inp.get("head") or None,
            task=inp.get("task") or "find_bugs",
            claim=inp.get("claim") or None,
            instructions=inp.get("instructions") or None,
            scope=inp.get("scope") or None,
            orchestrator=inp.get("orchestrator") or None,
        )
        return summarize_report(report, session.last_run_dir)

    def start_next_round(_: dict[str, Any]) -> dict[str, Any]:
        if session.last_report is None:
            return {"error": "no review has run yet; call start_review"}
        return summarize_report(session.run_next_round(), session.last_run_dir)

    def verdict(inp: dict[str, Any]) -> dict[str, Any]:
        if session.last_report is None or session.last_run_dir is None:
            return {"error": "no review has run yet"}
        rec = record_verdict(
            session.last_run_dir, inp["id"], inp["verdict"], inp.get("note") or ""
        )
        st = round_status(session.last_report, load_verdicts(session.last_run_dir))
        return {
            "finding_id": rec["finding_id"],
            "verdict": rec["verdict"],
            "validator_verdict": rec.get("agent_verdict"),
            "pending": st.pending,
            "passed": st.passed,
            "can_continue": st.can_continue,
        }

    def list_findings(_: dict[str, Any]) -> dict[str, Any]:
        if session.last_report is None:
            return {"error": "no review has run yet; call start_review"}
        return summarize_report(session.last_report, session.last_run_dir, limit=100)

    def finding_detail(inp: dict[str, Any]) -> dict[str, Any]:
        if session.last_report is None:
            return {"error": "no review has run yet"}
        for vf in session.last_report.findings:
            if vf.finding.id == inp["id"]:
                d = vf.model_dump()
                for ev in d.get("evidence", []):
                    if ev.get("output"):
                        ev["output"] = ev["output"][:3000]
                return d
        return {"error": f"unknown finding id {inp['id']}"}

    def read_file(inp: dict[str, Any]) -> str:
        p = (_root() / inp["path"]).resolve()
        if not str(p).startswith(str(_root().resolve())):
            raise ValueError("path escapes the repository")
        lines = split_lines(p.read_text(encoding="utf-8", errors="replace"))
        start = max(1, int(inp.get("start_line") or 1))
        end = min(len(lines), int(inp.get("end_line") or start + 199))
        return "\n".join(f"{i:5d}| {lines[i - 1]}" for i in range(start, end + 1))

    def search_code(inp: dict[str, Any]) -> str:
        limit = int(inp.get("max_results") or 40)
        if shutil.which("rg"):
            proc = subprocess.run(
                [
                    "rg",
                    "-n",
                    "--no-heading",
                    "--color",
                    "never",
                    "-m",
                    str(limit),
                    "-e",
                    inp["pattern"],
                ],
                cwd=str(_root()),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            return proc.stdout[:15000] or "(no matches)"
        proc = subprocess.run(
            ["git", "grep", "-n", "-E", inp["pattern"]],
            cwd=str(_root()),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return "\n".join(proc.stdout.splitlines()[:limit]) or "(no matches)"

    def git_log(inp: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            c.model_dump()
            for c in g.recent_commits(_root(), "HEAD", int(inp.get("n") or 10))
        ]

    def project_info(_: dict[str, Any]) -> dict[str, Any]:
        return session.project_info()

    def memory_read(_: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = session.memory.summary()
        return summary

    def memory_note(inp: dict[str, Any]) -> dict[str, Any]:
        path = session.memory.append_note(
            inp["text"], section=inp.get("section") or "Notes", author="lead"
        )
        return {"written": str(path), "section": inp.get("section") or "Notes"}

    def skill_list(_: dict[str, Any]) -> list[dict[str, Any]]:
        library = getattr(
            session, "skills", None
        )  # skills/shared_skills and skills/lead
        return library.summary("lead") if library is not None else []

    def skill_read(inp: dict[str, Any]) -> dict[str, Any]:
        library = getattr(session, "skills", None)
        skill = (
            library.get(str(inp.get("name") or ""), "lead")
            if library is not None
            else None
        )
        if skill is None:
            return {
                "error": f"unknown skill {inp.get('name')!r}; "
                "skill_list shows what is available"
            }
        return {
            "name": skill.name,
            "description": skill.description,
            "body": skill.body,
        }

    return [
        ToolSpec(
            "start_review",
            "Run the full multi-agent review. Omit base/head to use the session scope. "
            "scope 'all' reviews the whole repository at head instead of a diff. "
            "orchestrator 'openflow' lets a model run the reviewers and the validator "
            "as tools instead of the fixed flow; only when the person asks for it.",
            _obj(
                {
                    "base": {"type": "string"},
                    "head": {"type": "string"},
                    "task": {"type": "string", "enum": ["find_bugs", "verify_fix"]},
                    "claim": {"type": "string"},
                    "instructions": {"type": "string"},
                    "scope": {"type": "string", "enum": ["range", "all"]},
                    "orchestrator": {
                        "type": "string",
                        "enum": ["deterministic", "openflow"],
                    },
                }
            ),
            start_review,
            parallel_safe=False,
        ),
        ToolSpec(
            "record_verdict",
            "Record the person's own verdict on a finding of the last review: accept "
            "(a real problem) or reject (not one). Only when they said so.",
            _obj(
                {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["accept", "reject"]},
                    "note": {"type": "string"},
                },
                ["id", "verdict"],
            ),
            verdict,
        ),
        ToolSpec(
            "start_next_round",
            "Review the last review's pending findings again, from the planner, with "
            "its review report and the validator's comment report as context. Only "
            "when the person agreed to another round.",
            _obj({}),
            start_next_round,
            parallel_safe=False,
        ),
        ToolSpec(
            "list_findings",
            "Findings of the last review with ids and verdicts.",
            _obj({}),
            list_findings,
        ),
        ToolSpec(
            "finding_detail",
            "Full detail, evidence and fix of one finding.",
            _obj({"id": {"type": "string"}}, ["id"]),
            finding_detail,
            untrusted=True,
        ),
        ToolSpec(
            "read_file",
            "Read numbered lines of a file in the repository.",
            _obj(
                {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                ["path"],
            ),
            read_file,
            untrusted=True,
        ),
        ToolSpec(
            "search_code",
            "Regex search over the repository.",
            _obj(
                {"pattern": {"type": "string"}, "max_results": {"type": "integer"}},
                ["pattern"],
            ),
            search_code,
            untrusted=True,
        ),
        ToolSpec(
            "git_log",
            "Recent commits.",
            _obj({"n": {"type": "integer"}}),
            git_log,
            untrusted=True,
        ),
        ToolSpec(
            "project_info",
            "Repository facts and the current review scope.",
            _obj({}),
            project_info,
            untrusted=True,
        ),
        ToolSpec(
            "memory_read",
            "Project memory (.scr_agents/memory.md, previous runs, rejected findings).",
            _obj({}),
            memory_read,
            untrusted=True,
        ),
        ToolSpec(
            "memory_note",
            "Append a note to the project memory (.scr_agents/memory.md) so future "
            "reviews know it. Use when the person asks you to remember something, or "
            "states a convention, a false positive, or how to build/test.",
            _obj(
                {
                    "text": {"type": "string"},
                    "section": {
                        "type": "string",
                        "enum": [
                            "Build and test",
                            "Conventions",
                            "Known false positives",
                            "Notes",
                        ],
                    },
                },
                ["text"],
            ),
            memory_note,
        ),
        ToolSpec(
            "skill_list",
            "Operator-written review guidance (skills): names, descriptions, "
            "languages.",
            _obj({}),
            skill_list,
        ),
        ToolSpec(
            "skill_read",
            "Full text of one skill by name; pass its advice on or apply it when "
            "answering.",
            _obj({"name": {"type": "string"}}, ["name"]),
            skill_read,
        ),
    ]


class LeadAgent:
    """Answer a person in an interactive session, running reviews when asked.

    Each answer is one tool loop that sees the project facts, a summary of the last
    review and the last ``HISTORY_MESSAGES`` messages of the conversation, each cut to
    ``HISTORY_CHARS`` characters.
    """

    HISTORY_MESSAGES = 6  # earlier messages of the conversation each answer sees
    HISTORY_CHARS = 600  # each cut to this many characters

    def __init__(
        self,
        session: SessionLike,
        backend: LLMBackend,
        registry: PromptRegistry,
        safety: SafetyFilter,
        log: ActivityLog,
        max_turns: int = 12,
        max_tokens: int = 8000,
    ):
        """Set up the agent for one session; the conversation history starts empty.

        Args:
            session: The interactive session the tools act on.
            backend: The model backend that answers.
            registry: Approved prompts; the ``lead_agent`` prompt is loaded from it.
            safety: Filter for the untrusted content that reaches the model.
            log: Activity log that receives the lead's events.
            max_turns: Turn limit of each answer's tool loop.
            max_tokens: Token limit of each model reply.
        """
        self.session = session
        self.backend = backend
        self.registry = registry
        self.safety = safety
        self.log = log
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self.history: list[tuple[str, str]] = []

    def _context(self) -> str:
        # commit messages inside project_info come from the repository: data, never
        # instructions
        project = self.safety.wrap(
            json.dumps(self.session.project_info(), default=str), "project_info", 3000
        )
        last = ""
        if self.session.last_report is not None:
            s = summarize_report(
                self.session.last_report, self.session.last_run_dir, limit=10
            )
            last = "Last review:\n" + json.dumps(s, indent=1)
        hist = "\n".join(
            f"{who}: {text[: self.HISTORY_CHARS]}"
            for who, text in self.history[-self.HISTORY_MESSAGES :]
        )
        return (
            f"Project: {project}\n"
            f"Interface: {getattr(self.session, 'interface', 'terminal')}\n"
            f"Current scope: {self.session.scope} "
            f"{self.session.base}..{self.session.head}\n"
            f"{last}\n" + (f"Recent conversation:\n{hist}\n" if hist else "")
        )

    def _log_memory(self) -> None:
        """What of the conversation this answer is given.

        The rest is in the log, not in the prompt.
        """
        window = self.history[-self.HISTORY_MESSAGES :]
        last = self.session.last_report
        self.log.log(
            "memory_used",
            "lead",
            kind="conversation",
            messages_total=len(self.history),
            messages_used=len(window),
            chars_cap=self.HISTORY_CHARS,
            cut=sum(len(t) > self.HISTORY_CHARS for _, t in window),
            used=[
                {"role": who, "chars": len(t), "preview": " ".join(t.split())[:100]}
                for who, t in window
            ],
            last_review=last.run_id if last is not None else None,
            last_review_findings=len(last.findings) if last is not None else 0,
        )

    def ask(self, text: str) -> tuple[str, LoopResult]:
        """Answer one message and add it and the reply to the conversation history.

        Returns:
            The reply text and the tool loop result.
        """
        system = self.registry.load("lead_agent")
        loop = ToolLoop(
            self.backend,
            lead_tools(self.session, self.safety),
            self.safety,
            self.log,
            agent="lead",
            max_turns=self.max_turns,
            max_tokens=self.max_tokens,
        )
        user = f"{self._context()}\nUser message:\n{text}"
        self._log_memory()
        result = loop.run(system, user, output_schema=None)
        reply = result.text.strip() or (
            f"(no reply: {result.error})" if result.error else "(no reply)"
        )
        self.history.append(("user", text))
        self.history.append(("assistant", reply))
        return reply, result
