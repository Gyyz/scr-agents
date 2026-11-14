"""Interactive terminal session with the lead agent about one project.

Point it at a project (path or URL), talk to the lead agent, watch the agents work in
the terminal, and drill into results with slash commands.
"""

from __future__ import annotations

import json
import shlex
import threading
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from scr_agents.config import Settings, deep_merge
from scr_agents.inputs.project import default_scope, describe_repo, resolve_project
from scr_agents.inputs.request import build_request
from scr_agents.models import EvidenceReport, new_id
from scr_agents.pipeline import make_backend_factory, run_next_round, run_review
from scr_agents.sessions import ensure_session
from scr_agents.tui.render import VERDICT_STYLE, LiveRenderer
from scr_agents.ws1_review.lead import LeadAgent
from scr_agents.ws1_review.rounds import RoundError
from scr_agents.ws4_guardrails.registry import PromptRegistry
from scr_agents.ws4_guardrails.safety_filter import SafetyFilter
from scr_agents.ws5_knowledge.activity_log import ActivityLog
from scr_agents.ws5_knowledge.human_review import record_verdict
from scr_agents.ws5_knowledge.project_memory import ProjectMemory
from scr_agents.ws5_knowledge.skills import SkillLibrary

HELP = (
    'Talk in plain language ("check the last 3 commits for memory bugs", '
    '"verify that abc123 fixes the\n'
    'overflow", "review the whole project for injection", '
    '"what does parse_header do?").\n'
    "\n"
    "Slash commands:\n"
    "  /review [base..head | all]   run a review on the current or given scope\n"
    "  /scope [base..head | all]    show or change the scope\n"
    "  /findings                    findings of the last run\n"
    "  /show <id>                   full detail and evidence of one finding\n"
    "  /verdict <id> accept|reject [note]   record a human verdict\n"
    "  /next                        review the last run's pending findings again\n"
    "  /log [n]                     last n events of the last run (default 40)\n"
    "  /runs                        runs of this session\n"
    "  /memory                      show the project memory"
    " (.scr_agents/ in the repo)\n"
    "  /remember <text>             append a note to the project memory\n"
    "  /web                         start the web console for the detailed view\n"
    "  /verbose                     toggle tool-output previews in the live view\n"
    "  /help  /quit\n"
)


class ChatSession:
    """A terminal conversation with the lead agent about one project.

    Implements the lead agent's SessionLike protocol: reviews run in this process
    and render live in the terminal.
    """

    def __init__(
        self,
        settings: Settings,
        target: str,
        mode: str | None = None,
        console: Console | None = None,
        base: str | None = None,
        head: str | None = None,
        scope: str | None = None,
    ):
        """Open the project and start a new ``chat`` session with the lead agent.

        Args:
            settings: The settings to run with.
            target: Path or git URL of the project; a URL is cloned (or fetched)
                into ``projects_dir`` first.
            mode: LLM mode for this session, instead of the configured one.
            console: Where to print; a new ``Console`` when not given.
            base: Base ref of the starting scope.
            head: Head ref of the starting scope.
            scope: ``"range"`` or ``"all"``. Left out, the scope is the last
                commit, or the whole tree when there is only one commit.
        """
        if mode:
            settings = Settings.model_validate(
                deep_merge(settings.model_dump(), {"llm": {"mode": mode}})
            )
        self.settings = settings
        self.console = console or Console()
        self.repo = resolve_project(target, Path(settings.projects_dir))
        self.memory = ProjectMemory(self.repo)
        self.skills = SkillLibrary(settings.skill_paths())
        b, h, s = default_scope(self.repo)
        self.base, self.head, self.scope = base or b, head or h, scope or s
        self.session_id = new_id("session")
        self.session_dir = ensure_session(
            settings, self.session_id, kind="chat", repo=str(self.repo)
        )
        self.log = ActivityLog(self.session_dir / "activity.jsonl", self.session_id, [])
        self.registry = PromptRegistry(
            settings.manifest_path(),
            settings.registry.require_approved,
            stats_path=Path(settings.output_dir).resolve() / "registry_stats.jsonl",
        )
        self.safety = SafetyFilter(settings.safety)
        self.factory, self.mode = make_backend_factory(settings)
        self.lead = LeadAgent(
            self,
            self.factory("lead", None),
            self.registry,
            self.safety,
            self.log,
            max_turns=settings.llm.max_turns,
        )
        self.reports: list[tuple[EvidenceReport, Path]] = []
        self.interface = "terminal"  # tells the lead agent how findings open
        self.last_report: EvidenceReport | None = None
        self.last_run_dir: Path | None = None
        self.verbose = False
        self._renderer: LiveRenderer | None = None
        self._web: dict[str, Any] | None = None
        self.log.subscribe(self._on_session_event)

    # ------------------------------------------------------------------ SessionLike
    def project_info(self) -> dict[str, Any]:
        """Describe the repository, the session's current scope and the LLM mode."""
        info = describe_repo(self.repo)
        info.update(
            {
                "scope": self.scope,
                "base": self.base,
                "head": self.head,
                "mode": self.mode,
            }
        )
        return info

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
        """Run a review in this process, show it live, then print its findings.

        Arguments left out keep the session's current scope, and the scope
        reviewed becomes the current one. A range whose base and head are the
        same reviews the whole tree.

        Args:
            base: Base ref, or ``None`` for the current one.
            head: Head ref, or ``None`` for the current one.
            task: What the reviewers are asked to do.
            claim: Optional claim to check.
            instructions: Optional extra instructions for the reviewers.
            scope: ``"range"`` or ``"all"``, or ``None`` for the current one.
            orchestrator: ``"deterministic"`` or ``"openflow"`` for this review
                only; ``None`` keeps ``review.orchestrator``.

        Returns:
            The finished report.
        """
        base, head, scope = base or self.base, head or self.head, scope or self.scope
        if base == head and scope != "all":
            scope = "all"
        request = build_request(
            self.repo,
            base,
            head,
            task=task,
            claim=claim,
            instructions=instructions,
            scope=scope,
            max_files=self.settings.whole_repo_max_files,
        )
        own = self._renderer is None
        renderer = self._renderer or LiveRenderer(self.console, verbose=self.verbose)
        if own:
            renderer.__enter__()
        try:
            settings = self.settings
            if orchestrator:
                settings = settings.model_copy(deep=True)
                settings.review.orchestrator = orchestrator  # type: ignore[assignment]
            report, run_dir = run_review(
                settings,
                self.repo,
                base,
                head,
                request=request,
                on_event=renderer.handle,
                session_id=self.session_id,
            )
        finally:
            if own:
                renderer.__exit__(None, None, None)
        renderer.print_summary(report, run_dir)
        self.reports.append((report, run_dir))
        self.last_report, self.last_run_dir = report, run_dir
        self.base, self.head, self.scope = base, head, scope
        return report

    def run_next_round(self) -> EvidenceReport:
        """Run the next review round of the last review in this process, shown live.

        Returns:
            The new round's report.

        Raises:
            RoundError: If there is no review yet, it passed, or it reached the round
                limit.
        """
        if self.last_run_dir is None:
            raise RoundError("no review has run yet")
        own = self._renderer is None
        renderer = self._renderer or LiveRenderer(self.console, verbose=self.verbose)
        if own:
            renderer.__enter__()
        try:
            report, run_dir = run_next_round(
                self.settings, self.last_run_dir, on_event=renderer.handle
            )
        finally:
            if own:
                renderer.__exit__(None, None, None)
        renderer.print_summary(report, run_dir)
        self.reports.append((report, run_dir))
        self.last_report, self.last_run_dir = report, run_dir
        return report

    # ------------------------------------------------------- events from the lead agent
    def _on_session_event(self, e: dict[str, Any]) -> None:
        # the review itself renders its own lines; show the lead agent's other tool
        # calls
        if (
            self._renderer is not None
            and e.get("event") in ("tool_call", "llm_error")
            and e.get("tool") != "start_review"
        ):
            self._renderer.handle(e)

    # ------------------------------------------------------------------ interaction
    def ask(self, text: str) -> str:
        """Send ``text`` to the lead agent, show its work live and print the reply.

        Returns:
            The lead agent's reply.
        """
        with LiveRenderer(self.console, verbose=self.verbose) as renderer:
            self._renderer = renderer
            try:
                reply, result = self.lead.ask(text)
            finally:
                self._renderer = None
        if result.error and not result.text:
            self.console.print(Text(f"lead agent error: {result.error}", style="red"))
        self.console.print(
            Panel(
                Markdown(reply),
                title="SCR Agents",
                title_align="left",
                border_style="medium_purple",
            )
        )
        return reply

    def handle(self, line: str) -> bool:
        """Handle one input line. Returns False when the session should end."""
        try:
            return self._handle(line)
        except Exception as e:
            self.console.print(Text(f"error: {type(e).__name__}: {e}", style="red"))
            return True

    def _handle(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return True
        if not line.startswith("/"):
            self.ask(line)
            return True
        head, _, rest = line.partition(" ")
        cmd, rest = head.lower(), rest.strip()
        try:
            args = shlex.split(rest)
        except ValueError:  # an apostrophe in free text is not an open quote
            args = rest.split()
        if cmd in ("/quit", "/exit", "/q"):
            return False
        if cmd == "/help":
            self.console.print(HELP)
        elif cmd == "/scope":
            if args:
                self._set_scope(args[0])
            self.console.print(
                f"scope: {self.scope} {self.base}..{self.head}  repo: {self.repo}"
            )
        elif cmd == "/review":
            if args:
                self._set_scope(args[0])
            self.run_review()
        elif cmd == "/findings":
            self._findings()
        elif cmd == "/show":
            self._show(args[0] if args else "")
        elif cmd == "/verdict":
            self._verdict(args)
        elif cmd == "/next":
            try:
                self.run_next_round()
            except RoundError as e:
                self.console.print(str(e), style="yellow")
        elif cmd == "/log":
            self._log(int(args[0]) if args else 40)
        elif cmd == "/runs":
            for report, run_dir in self.reports:
                c = report.counts()
                self.console.print(
                    f"{report.run_id}  {report.request.base[:10]}.."
                    f"{report.request.head[:10]}  "
                    f"{c['confirmed']}✓ {c['unverified']}? {c['rejected']}✗  {run_dir}"
                )
            if not self.reports:
                self.console.print("no runs yet")
        elif cmd == "/memory":
            s = self.memory.summary()
            self.console.print(
                Panel(
                    Markdown(
                        s["notes"]
                        or "_no notes yet; use /remember or edit .scr_agents/memory.md_"
                    ),
                    title=f"project memory · {s['dir']}",
                    title_align="left",
                    border_style="dodger_blue1",
                )
            )
            if s["runs"]:
                self.console.print(
                    Text(
                        f"{s['findings_recorded']} findings recorded over "
                        f"{len(s['runs'])} run(s); "
                        f"{len(s['human_rejected'])} rejected by humans",
                        style="grey62",
                    )
                )
        elif cmd == "/remember":
            if not rest:
                self.console.print("usage: /remember <text>")
            else:
                p = self.memory.append_note(rest, author="human")
                self.console.print(f"noted in {p}")
        elif cmd == "/web":
            self._web_console()
        elif cmd == "/verbose":
            self.verbose = not self.verbose
            self.console.print(f"verbose {'on' if self.verbose else 'off'}")
        else:
            self.console.print(f"unknown command {cmd}; /help lists the commands")
        return True

    def _set_scope(self, spec: str) -> None:
        if spec == "all":
            self.scope = "all"
        elif ".." in spec:
            self.base, self.head = spec.split("..", 1)
            self.head = self.head or "HEAD"
            self.scope = "range"
        else:
            self.base, self.head, self.scope = spec, "HEAD", "range"

    def _findings(self) -> None:
        if self.last_report is None:
            self.console.print(
                "no review has run yet; type what to check or use /review"
            )
            return
        for vf in self.last_report.findings:
            f = vf.finding
            t = Text(f"{f.id}  ")
            t.append(vf.verdict.value, style=VERDICT_STYLE.get(vf.verdict.value, ""))
            t.append(f"  {f.severity.value:8s} {f.title}  ")
            t.append(f"{f.location.path}:{f.location.line or '?'}", style="cyan")
            self.console.print(t)
        if not self.last_report.findings:
            self.console.print("no findings")

    def _show(self, fid: str) -> None:
        if self.last_report is None:
            self.console.print("no review has run yet")
            return
        for vf in self.last_report.findings:
            if vf.finding.id == fid:
                f = vf.finding
                meta = (
                    f"verdict **{vf.verdict.value}** · severity {f.severity.value} · "
                    f"{f.category.value} · "
                    f"confidence {f.confidence:.2f} · source {f.source}"
                )
                body = [
                    f"**{f.title}**",
                    "",
                    meta,
                    f"`{f.location.path}:{f.location.line or '?'}`"
                    + (f" · {', '.join(f.cwe)}" if f.cwe else ""),
                    "",
                    f.description,
                    "",
                ]
                if f.rationale:
                    body += [f"*Rationale.* {f.rationale}", ""]
                if vf.validator_notes:
                    body += [f"*Validator.* {vf.validator_notes}", ""]
                for ev in vf.evidence:
                    body.append(
                        f"- evidence ({ev.kind}"
                        f"{', ' + ev.ref if ev.ref else ''}): {ev.summary}"
                        + (f" · `{ev.command}`" if ev.command else "")
                        + (
                            f" · exit {ev.exit_code}"
                            if ev.exit_code is not None
                            else ""
                        )
                    )
                    if ev.output:
                        body += ["", "```", ev.output[:2500], "```", ""]
                fix = vf.fix or f.suggested_fix
                if fix:
                    body += ["*Suggested fix.*", "", "```diff", fix[:3000], "```"]
                self.console.print(
                    Panel(
                        Markdown("\n".join(body)),
                        title=f.id,
                        title_align="left",
                        border_style=VERDICT_STYLE.get(vf.verdict.value, "white"),
                    )
                )
                return
        self.console.print(f"unknown finding id {fid!r}; /findings lists them")

    def _verdict(self, args: list[str]) -> None:
        if self.last_run_dir is None or len(args) < 2:
            self.console.print(
                "usage: /verdict <id> accept|reject [note]  (after a review)"
            )
            return
        rec = record_verdict(
            self.last_run_dir, args[0], args[1], " ".join(args[2:]), self.registry
        )
        self.console.print(
            f"recorded {rec['verdict']} for {rec['finding_id']} "
            f"(agent said {rec['agent_verdict']})"
        )

    def _log(self, n: int) -> None:
        if self.last_run_dir is None:
            self.console.print("no review has run yet")
            return
        events = ActivityLog.read(self.last_run_dir / "activity.jsonl")
        for e in events[-n:]:
            extra = {
                k: v
                for k, v in e.items()
                if k not in ("seq", "ts", "run_id", "commit_ids", "event", "agent")
            }
            t = Text(f"{e.get('ts', '')[11:19]} ", style="grey50")
            t.append(f"{(e.get('agent') or 'pipeline'):28s} ", style="medium_purple")
            t.append(f"{e.get('event'):16s} ", style="bold")
            t.append(json.dumps(extra, default=str)[:160], style="grey62")
            self.console.print(t)
        self.console.print(
            Text(
                f"{len(events)} events in {self.last_run_dir / 'activity.jsonl'}",
                style="grey50",
            )
        )

    def _web_console(self) -> None:
        if self._web is not None:
            self.console.print(f"web console: {self._web['url']}")
            return
        try:
            import uvicorn

            from scr_agents.web.server import create_app
        except ImportError:
            self.console.print("web console needs the 'web' extra: uv sync --extra web")
            return
        port = 8765
        config = uvicorn.Config(
            create_app(self.settings), host="127.0.0.1", port=port, log_level="warning"
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True, name="scr-agents-web")
        thread.start()
        self._web = {"url": f"http://127.0.0.1:{port}", "server": server}
        self.console.print(
            f"web console: {self._web['url']}  "
            "(runs of this session appear in the list)"
        )

    # ------------------------------------------------------------------ main loop
    def banner(self) -> None:
        """Print the project, its branch and head, the scope and the LLM mode."""
        info = describe_repo(self.repo)
        self.console.print(
            Panel(
                f"[bold]{self.repo.name}[/]  {info['branch']} @ {info['head'][:10]}  "
                f"·  {info['files']} files\n"
                f"scope: {self.scope} {self.base}..{self.head}   mode: {self.mode}\n"
                f"[grey62]Tell me what to check, or /help. Ctrl-D to quit.[/]",
                title="SCR Agents",
                border_style="medium_purple",
            )
        )

    def loop(self) -> None:
        """Read and handle input lines until ``/quit``, end of input or Ctrl-C.

        Closes the session log and stops the web console, if started, on the way
        out.
        """
        self.banner()
        try:
            while True:
                try:
                    line = Prompt.ask("[bold medium_purple]›[/]", console=self.console)  # noqa: RUF001
                except (EOFError, KeyboardInterrupt):
                    break
                if not self.handle(line):
                    break
        finally:
            self.log.close()
            if self._web is not None:
                self._web["server"].should_exit = True
