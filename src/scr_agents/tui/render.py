"""Live terminal renderer fed by activity-log events.

Finished steps are printed into the scrollback (one line each, Claude Code style: a dot,
the actor, the action, and a dimmed result on the next line); a small live region at the
bottom shows the pipeline stages and the agents currently working.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Self

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from scr_agents.models import EvidenceReport

STAGES = [
    ("inputs", "inputs"),
    ("graph", "code graph"),
    ("scanners", "scanners"),
    ("bench", "test bench"),
    ("review", "review"),
    ("validation", "validation"),
    ("report", "report"),
]
WS_STYLE = {
    "ws1": "medium_purple",
    "ws2": "green",
    "ws3": "red",
    "ws4": "yellow",
    "ws5": "dodger_blue1",
    "ext": "grey62",
}
TOOL_WS = {
    "read_file": "ext",
    "get_diff": "ext",
    "list_changed_files": "ext",
    "search_code": "ext",
    "git_log": "ext",
    "code_graph_query": "ws2",
    "scanner_findings": "ws2",
    "run_tests": "ws3",
    "run_command": "ws3",
    "web_search": "ws4",
    "kb_lookup": "ws5",
    "start_review": "ws1",
    "finding_detail": "ws5",
    "list_findings": "ws5",
    "project_info": "ext",
}
VERDICT_STYLE = {"confirmed": "bold red", "unverified": "yellow", "rejected": "grey58"}


def _args(inp: dict[str, Any], n: int = 70) -> str:
    s = ", ".join(
        f"{k}={v if isinstance(v, str) else json.dumps(v)}"
        for k, v in (inp or {}).items()
    )
    return s if len(s) <= n else s[:n] + "…"


class LiveRenderer:
    """Show a review's activity-log events in the terminal as they arrive.

    Use it as a context manager to display the live region, and pass each event
    to ``handle``.
    """

    def __init__(self, console: Console | None = None, verbose: bool = False):
        """Set up the renderer; nothing is shown until the context is entered.

        Args:
            console: Where to print; a new ``Console`` when not given.
            verbose: Also show tool output previews, model text and events the
                renderer has no line for.
        """
        self.console = console or Console()
        self.verbose = verbose
        self._lock = threading.Lock()
        self._live: Live | None = None
        self.stages: dict[str, dict[str, Any]] = {}
        self.active: dict[str, str] = {}  # agent -> what it is doing
        self.counts: dict[str, int] = {"tools": 0, "llm_calls": 0}

    # ------------------------------------------------------------------ live region
    def _status(self) -> Group:
        stage = Text()
        for key, label in STAGES:
            e = self.stages.get(key)
            status = e["status"] if e else "pending"
            glyph = {"done": "✓ ", "running": "⠿ ", "pending": "· "}[status]
            style = {"done": "green", "running": "bold yellow", "pending": "grey50"}[
                status
            ]
            stage.append(glyph + label + "   ", style=style)
        rows: list[Any] = [stage]
        for agent, doing in list(self.active.items())[:8]:
            t = Table.grid(padding=(0, 1))
            t.add_row(
                Spinner("dots", style="yellow"),
                Text(agent, style=WS_STYLE["ws1"]),
                Text(doing, style="grey62"),
            )
            rows.append(t)
        return Group(*rows)

    def __enter__(self) -> Self:
        """Start the live region at the bottom of the terminal."""
        self._live = Live(
            self._status(), console=self.console, refresh_per_second=8, transient=True
        )
        self._live.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        """Remove the live region; the lines already printed stay."""
        if self._live is not None:
            self._live.__exit__(*exc)  # type: ignore[arg-type]
            self._live = None

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._status())

    def _print(self, renderable: Any) -> None:
        self.console.print(renderable)

    # ------------------------------------------------------------------ events
    def handle(self, e: dict[str, Any]) -> None:
        """Render one activity-log event and refresh the live region; thread-safe."""
        with self._lock:
            try:
                self._handle(e)
            finally:
                self._refresh()

    def _line(
        self,
        actor: str,
        action: str,
        style: str = "ws1",
        glyph: str = "⏺",
        indent: int = 0,
    ) -> None:
        t = Text("  " * indent)
        t.append(glyph + " ", style=WS_STYLE.get(style, style))
        t.append(actor + "  ", style="bold")
        t.append(action)
        self._print(t)

    def _result(self, text: str, indent: int = 1, style: str = "grey58") -> None:
        t = Text("  " * indent + "⎿  ", style="grey50")
        t.append(text, style=style)
        self._print(t)

    def _handle(self, e: dict[str, Any]) -> None:  # one branch per event type
        t = e.get("event")
        agent = e.get("agent") or "pipeline"
        if t == "stage":
            self.stages[e["stage"]] = e
            if e["status"] == "done":
                extra = {
                    k: v
                    for k, v in e.items()
                    if k
                    not in (
                        "seq",
                        "ts",
                        "run_id",
                        "commit_ids",
                        "event",
                        "agent",
                        "stage",
                        "status",
                    )
                }
                self._line(
                    "pipeline",
                    f"{e['stage']} done" + (f"  {_args(extra, 90)}" if extra else ""),
                    "ws2",
                )
            return
        if t == "request":
            self.stages.clear()
            self._line(
                "pipeline",
                (
                    f"review {e.get('run_id')}: {e.get('files')} files, "
                    f"{e.get('commits')} commits, task {e.get('task')}"
                ),
                "ext",
            )
            if e.get("claim"):
                self._result(f"claim: {e['claim']}")
            return
        if t == "mode":
            self._line(
                "pipeline",
                (
                    f"mode {e.get('mode')} · reviewer {e.get('reviewer_model')} · "
                    f"validator {e.get('validator_model')}"
                ),
                "ws4",
            )
            return
        if t == "code_graph":
            self._result(e.get("summary", ""))
            return
        if t == "scanners":
            self._result(
                f"{e.get('findings')} scanner hits · ran {', '.join(e.get('run', []))}"
                + (f" · missing {', '.join(e['missing'])}" if e.get("missing") else "")
            )
            return
        if t == "test_bench":
            self._result(e.get("delta", ""))
            return
        if t == "plan":
            p = e.get("plan") or {}
            self._line(
                "review_agent",
                (
                    f"plan: {len(p.get('focus_files', []))} focus files, "
                    f"{len(p.get('hypotheses', []))} hypotheses"
                ),
            )
            for h in (p.get("hypotheses") or [])[:5]:
                self._result(f"hypothesis: {h}")
            return
        if t in ("subagent_start", "validate_start"):
            name = agent if t == "subagent_start" else f"validator:{e.get('finding')}"
            self.active[name] = "starting"
            self._line(name, f"started ({e.get('model')})")
            return
        if t == "llm_start":
            self.active[agent] = f"turn {e.get('turn')} · thinking"
            return
        if t == "tool_start":
            self.active[agent] = f"running {e.get('tool')}"
            return
        if t == "llm_call":
            self.counts["llm_calls"] += 1
            tools = e.get("tool_uses") or []
            self.active[agent] = f"turn {e.get('turn')} · " + (
                f"calling {', '.join(tools)}" if tools else e.get("stop_reason", "")
            )
            if self.verbose and e.get("text_preview") and not tools:
                self._result(e["text_preview"][:200].replace("\n", " "), style="grey62")
            return
        if t == "tool_call":
            self.counts["tools"] += 1
            ws = (
                "ws4"
                if str(e.get("tool", "")).startswith("mcp_")
                else TOOL_WS.get(e.get("tool", ""), "ws1")
            )
            self._line(
                agent, f"{e.get('tool')}({_args(e.get('input') or {})})", ws, indent=1
            )
            preview = (e.get("output_preview") or "").replace("\n", " ⏎ ")[:120]
            res = (
                f"{'ERROR ' if e.get('is_error') else ''}{e.get('output_chars')} chars"
                f" · {e.get('duration_s')}s"
            )
            if e.get("is_error") or self.verbose:
                res += f" · {preview}"
            self._result(res, indent=2, style="red" if e.get("is_error") else "grey58")
            return
        if t == "subagent_done":
            self.active.pop(agent, None)
            self._line(
                agent,
                f"done · {e.get('findings')} findings · {e.get('turns')} turns"
                + (f" · {e['error']}" if e.get("error") else ""),
                "ws1",
            )
            return
        if t == "candidates":
            self._line(
                "review_agent", f"{e.get('count')} candidate findings to validate"
            )
            return
        if t == "verdict":
            self.active.pop(f"validator:{e.get('finding')}", None)
            v = e.get("verdict", "?")
            tt = Text("⏺ ", style=WS_STYLE["ws1"])
            tt.append(f"validator:{e.get('finding')}  ", style="bold")
            tt.append(v, style=VERDICT_STYLE.get(v, ""))
            if e.get("error"):
                tt.append(f" · {e['error']}", style="red")
            self._print(tt)
            return
        if t == "report":
            self._line("pipeline", f"report written: {e.get('markdown')}", "ws3")
            return
        if t in ("llm_error", "run_error", "mcp_error", "run_failed"):
            self.active.pop(agent, None)
            self._line(agent, f"{t}: {e.get('error')}", "red", glyph="✗")
            return
        if self.verbose:
            self._result(
                f"{t} {
                    escape(
                        json.dumps(
                            {
                                k: v
                                for k, v in e.items()
                                if k not in ('seq', 'ts', 'run_id', 'commit_ids')
                            },
                            default=str,
                        )[:160]
                    )
                }"
            )

    # ------------------------------------------------------------------ summary
    def print_summary(self, report: EvidenceReport, run_dir: Any) -> None:
        """Print the findings table and where the report files are.

        Errors that left the run incomplete and the test bench result come after
        the table.

        Args:
            report: The finished run.
            run_dir: The run directory, shown as the report's location.
        """
        c = report.counts()
        order = {"confirmed": 0, "unverified": 1, "rejected": 2}
        table = Table(
            title=(
                f"Findings for {report.run_id}  ·  {c['confirmed']} confirmed, "
                f"{c['unverified']} unverified, {c['rejected']} rejected"
            ),
            show_lines=False,
            expand=False,
        )
        for col, style in (
            ("id", "bold"),
            ("verdict", ""),
            ("severity", ""),
            ("title", ""),
            ("location", "cyan"),
        ):
            table.add_column(col, style=style, overflow="fold")
        for vf in sorted(report.findings, key=lambda v: order.get(v.verdict.value, 9)):
            f = vf.finding
            table.add_row(
                f.id,
                Text(vf.verdict.value, style=VERDICT_STYLE.get(vf.verdict.value, "")),
                f.severity.value,
                f.title,
                f"{f.location.path}:{f.location.line or '?'}",
            )
        if not report.findings:
            table.add_row("-", "-", "-", "no findings", "-")
        self.console.print(table)
        for err in report.errors:
            self.console.print(Text(f"incomplete: {err}", style="red"))
        if report.bench:
            self.console.print(
                Text(f"test bench: {report.bench.delta}", style="grey62")
            )
        self.console.print(
            Text(
                f"report: {run_dir}/report.html (also .md, .json)   ·   "
                "/show <id> for evidence · /log for the full trace · "
                f"/web for the console",
                style="grey50",
            )
        )
        if Path(run_dir, "report.json").is_file():
            self.print_round(run_dir)

    def print_round(self, run_dir: Any) -> None:
        """Print the round a run reached, its two round reports and what comes next."""
        from scr_agents.ws1_review.rounds import run_status

        st = run_status(Path(run_dir))
        if st.passed:
            state, style = "passed: every finding is settled", "green"
        elif not st.can_continue:
            state, style = (
                f"{len(st.pending)} pending; round limit reached",
                "yellow",
            )
        else:
            state, style = (
                f"{len(st.pending)} pending ({len(st.disputed)} disputed, "
                f"{len(st.awaiting_verdict)} without your verdict) · /verdict, "
                "then /next for another round",
                "yellow",
            )
        self.console.print(
            Text(f"round {st.round} of {st.max_rounds}: {state}", style=style)
        )
        self.console.print(
            Text(
                f"{st.label_a}: {run_dir}/report_a.html   ·   "
                f"{st.label_b}: {run_dir}/report_b.html",
                style="grey50",
            )
        )
