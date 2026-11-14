"""Web chat: a conversation with the lead agent about one repository.

Each chat is a session (`out/sessions/<chat_id>/`, kind `chat-web`). Everything the
browser shows comes from the session's activity log: the messages, the lead agent's
model and tool calls, and markers for the reviews it starts (`run_started` / `run_done`,
whose own events stream from the run). So a chat replays from disk after a restart, and
can be resumed: the transcript is read back into the lead agent's memory.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

from scr_agents.config import Settings
from scr_agents.inputs.project import (
    default_scope,
    describe_repo,
    is_url,
    project_name,
    resolve_project,
)
from scr_agents.models import EvidenceReport
from scr_agents.pipeline import make_backend_factory
from scr_agents.sessions import (
    ensure_session,
    list_sessions,
    new_session,
    session_dir,
    session_meta,
)
from scr_agents.web.runs import LiveRun, RunManager
from scr_agents.ws1_review.lead import LeadAgent
from scr_agents.ws1_review.rounds import RoundError
from scr_agents.ws4_guardrails.registry import PromptRegistry
from scr_agents.ws4_guardrails.safety_filter import SafetyFilter
from scr_agents.ws5_knowledge.activity_log import ActivityLog
from scr_agents.ws5_knowledge.project_memory import ProjectMemory
from scr_agents.ws5_knowledge.skills import SkillLibrary

# one subscriber's records, in order
RecordQueue = queue.Queue[dict[str, Any]]

CHAT_KIND = "chat-web"


RUN_POLL_S = 0.5


class ChatBusyError(RuntimeError):
    """Raised when a conversation that is still working is asked to be deleted."""

    pass


class Feed:
    """Records of one chat, replayable, fanned out to every connected browser."""

    def __init__(self, records: list[dict[str, Any]] | None = None):
        """Start from ``records``: the transcript read back when a chat is restored."""
        self.records: list[dict[str, Any]] = list(records or [])
        self._subs: list[RecordQueue] = []
        self._lock = threading.Lock()

    def emit(self, rec: dict[str, Any]) -> None:
        """Keep a record and put it on every subscriber's queue."""
        with self._lock:
            self.records.append(rec)
            subs = list(self._subs)
        for q in subs:
            q.put(rec)

    def subscribe(self) -> tuple[list[dict[str, Any]], RecordQueue]:
        """Return the records so far and a queue that receives every later one.

        Both are taken under one lock, so no record is missed or repeated. Pass the
        queue to ``unsubscribe`` when done.
        """
        with self._lock:
            q: RecordQueue = queue.Queue()
            self._subs.append(q)
            return list(self.records), q

    def unsubscribe(self, q: RecordQueue) -> None:
        """Stop putting records on ``q``; an unknown queue is ignored."""
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)


def _read_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


class WebChat:
    """A conversation with the lead agent about one repository.

    Implements the lead agent's SessionLike protocol on top of the web run manager.
    """

    def __init__(
        self,
        settings: Settings,
        manager: RunManager,
        repo: Path | None,
        target: str,
        chat_id: str | None = None,
        first_message: str | None = None,
        orchestrator: str | None = None,
    ):
        """Create or restore a chat and start its thread.

        `repo` is the local checkout when it is already known; otherwise `target` (a
        URL or path) is resolved, and cloned if needed, on the chat's own thread, so
        creating a chat never waits for git.

        Passing ``chat_id`` restores that chat from its session: the transcript is
        replayed and read back into the lead agent's history. ``first_message`` is
        queued before the thread starts, so the introduction knows a request is
        waiting. ``orchestrator`` (``deterministic`` or ``openflow``) runs every
        review of the chat that does not name one itself; it is kept in the
        session, and ``None`` means ``review.orchestrator``.
        """
        self.settings = settings
        self.manager = manager
        self.repo: Path | None = Path(repo) if repo else None
        self.target = target
        self.memory: ProjectMemory | None = None
        self.interface = "web console"  # tells the lead agent how findings open
        self.last_report: EvidenceReport | None = None
        self.last_run_dir: Path | None = None
        self.base, self.head, self.scope = "HEAD~1", "HEAD", "range"
        self.ready = threading.Event()
        self.prepare_error: str | None = None
        restoring = chat_id is not None
        stored = (
            session_meta(settings, chat_id).get("orchestrator") if chat_id else None
        )
        self.orchestrator: str = orchestrator or stored or settings.review.orchestrator
        meta = {
            "repo": str(self.repo) if self.repo else "",
            "target": target,
            "orchestrator": self.orchestrator,
        }
        self.chat_id = chat_id or new_session(settings, CHAT_KIND, **meta)
        ensure_session(settings, self.chat_id, CHAT_KIND, **meta)
        log_path = session_dir(settings, self.chat_id) / "activity.jsonl"
        previous = _read_log(log_path) if restoring else []
        self.feed = Feed(previous)
        self.log = ActivityLog(log_path, self.chat_id, [], subscribers=[self.feed.emit])
        self.log._seq = max(
            (r.get("seq") or 0 for r in previous), default=0
        )  # continue the numbering
        factory, self.mode = make_backend_factory(settings)
        registry = PromptRegistry(
            settings.manifest_path(),
            settings.registry.require_approved,
            stats_path=Path(settings.output_dir).resolve() / "registry_stats.jsonl",
        )
        self.lead = LeadAgent(
            self,
            factory("lead", None),
            registry,
            SafetyFilter(settings.safety),
            self.log,
            max_turns=settings.llm.max_turns,
        )
        self.busy = True  # until the repository is ready
        self.runs: list[str] = []
        self._inbox: queue.Queue[str] = queue.Queue()
        self._has_intro = any(
            r.get("event") == "message" and r.get("intro") for r in previous
        )
        if restoring:
            self._restore(previous)
        if (
            first_message
        ):  # queued before the thread starts, so the intro knows a request is waiting
            self.send(first_message)
        threading.Thread(
            target=self._worker, daemon=True, name=f"chat-{self.chat_id}"
        ).start()

    @property
    def skills(self) -> SkillLibrary:
        """The lead agent's skills (shared_skills + lead).

        Read on each use so the console's switches apply.
        """
        return SkillLibrary(self.settings.skill_paths())

    @property
    def title(self) -> str:
        """The chat's title, stable before and after the clone.

        The repository name from the URL, else the folder name.
        """
        if self.target == "demo":
            return self.repo.name if self.repo else "demo"
        if is_url(self.target):
            return project_name(self.target).rsplit("-", 1)[0]
        return (self.repo or Path(self.target)).name

    # ---------------------------------------------------------- getting the repository
    def _prepare(self) -> bool:
        """Resolve (clone or fetch) the repository.

        Progress and failures are messages in the chat.
        """
        if self.ready.is_set():
            return True
        t0 = time.monotonic()
        if self.repo is None or not self.repo.exists():
            if not is_url(self.target):
                action = "open"
            else:  # an existing cache entry only needs a fetch
                url = (
                    self.target
                    if "://" in self.target or self.target.startswith("git@")
                    else f"https://github.com/{self.target}.git"
                )
                cached = (
                    Path(self.settings.projects_dir) / project_name(url) / ".git"
                ).exists()
                action = "update" if cached else "clone"
            self.log.log("repo_preparing", "chat", target=self.target, action=action)
            try:
                self.repo = resolve_project(
                    self.target, Path(self.settings.projects_dir)
                )
            except Exception as e:
                self.prepare_error = str(e)
                self.log.log(
                    "repo_failed",
                    "chat",
                    target=self.target,
                    error=str(e),
                    duration_s=round(time.monotonic() - t0, 2),
                )
                self.log.log(
                    "message",
                    "chat",
                    role="assistant",
                    error=str(e),
                    text=(
                        f"I could not get the repository `{self.target}`:\n\n"
                        f"```\n{str(e)[:1500]}\n```\n\n"
                        "Check the address and send another message to try again."
                    ),
                )
                return False
            self._update_meta(repo=str(self.repo))
        self.memory = ProjectMemory(self.repo)
        self.base, self.head, self.scope = default_scope(self.repo)
        info = describe_repo(self.repo)
        self.log.log(
            "repo_ready",
            "chat",
            repo=str(self.repo),
            files=info.get("files"),
            head=info.get("head"),
            branch=info.get("branch"),
            duration_s=round(time.monotonic() - t0, 2),
        )
        self.prepare_error = None
        self.ready.set()
        if not self._has_intro:
            self._intro(info, ask=self._inbox.empty())
            self._has_intro = True
        return True

    def _update_meta(self, **fields: Any) -> None:
        marker = session_dir(self.settings, self.chat_id) / "session.json"
        try:
            meta = json.loads(marker.read_text())
            meta.update(fields)
            marker.write_text(json.dumps(meta, indent=2, default=str) + "\n")
        except (OSError, json.JSONDecodeError):
            pass

    # ------------------------------------------------------------------ SessionLike
    def project_info(self) -> dict[str, Any]:
        """Describe the repository, the chat's current scope and the LLM mode."""
        info: dict[str, Any] = (
            describe_repo(self.repo)
            if self.repo is not None
            else {"repo": self.target, "ready": False}
        )
        info.update(
            {
                "scope": self.scope,
                "base": self.base,
                "head": self.head,
                "mode": self.mode,
                "orchestrator": self.orchestrator,
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
        """Run a review for the lead agent and wait for its report.

        The review goes through the run manager, so the console streams it like
        any other run; the chat's log gets ``run_started`` and ``run_done``
        markers. Arguments left out keep the chat's current scope, and the
        scope reviewed becomes the current one. A range whose base and head are
        the same reviews the whole tree.

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

        Raises:
            RuntimeError: If the review could not start (``GitError``) or did not
                finish with a report.
        """
        base, head, scope = base or self.base, head or self.head, scope or self.scope
        if base == head and scope != "all":
            scope = "all"
        orchestrator = orchestrator or self.orchestrator
        run = self.manager.start(
            str(self.repo),
            base,
            head,
            task=task,
            claim=claim,
            instructions=instructions,
            scope=scope,
            session_id=self.chat_id,
            orchestrator=orchestrator,
        )
        self.runs.append(run.run_id)
        self.log.log(
            "run_started",
            "chat",
            run=run.run_id,
            base=base,
            head=head,
            scope=scope,
            task=task,
            files=len(run.request.files),
            orchestrator=orchestrator,
        )
        report = self._wait(run)
        self.base, self.head, self.scope = base, head, scope
        return report

    def run_next_round(self) -> EvidenceReport:
        """Run the next review round of the chat's last review and wait for it.

        The round reviews the last review's pending findings again (see
        ``ws1_review.rounds``); the chat's log gets ``run_started`` (with
        ``round`` and ``previous_run``) and ``run_done``.

        Returns:
            The new round's report.

        Raises:
            RoundError: If there is no review yet, it passed, or it reached the
                round limit.
            RuntimeError: If the round did not finish with a report.
        """
        if self.last_run_dir is None or self.last_report is None:
            raise RoundError("no review has run yet")
        run = self.manager.start_next_round(self.last_run_dir)
        self.runs.append(run.run_id)
        r = run.request
        self.log.log(
            "run_started",
            "chat",
            run=run.run_id,
            base=r.base_ref,
            head=r.head_ref,
            scope=r.scope,
            task=r.task.value,
            files=len(r.files),
            round=self.last_report.round + 1,
            previous_run=self.last_report.run_id,
        )
        return self._wait(run)

    def _wait(self, run: LiveRun) -> EvidenceReport:
        """Wait for a run the chat started, log ``run_done`` and keep its report."""
        while run.status == "running":
            time.sleep(RUN_POLL_S)
        self.log.log(
            "run_done",
            "chat",
            run=run.run_id,
            status=run.status,
            counts=run.counts,
            error=run.error,
        )
        if run.status != "finished" or not run.run_dir:
            raise RuntimeError(f"review {run.run_id} failed: {run.error}")
        report = EvidenceReport.model_validate_json(
            (Path(run.run_dir) / "report.json").read_text()
        )
        self.last_report, self.last_run_dir = report, Path(run.run_dir)
        return report

    # ------------------------------------------------------------------ conversation
    def send(self, text: str) -> None:
        """Log the user's message and queue it for the lead agent; returns at once."""
        self.log.log("message", "chat", role="user", text=text)
        self._inbox.put(text)

    def _worker(self) -> None:
        self.log.log("chat_status", "chat", busy=True, phase="preparing")
        self._prepare()
        self.busy = False
        self.log.log("chat_status", "chat", busy=False)
        while True:
            text = self._inbox.get()
            self.busy = True
            self.log.log("chat_status", "chat", busy=True)
            try:
                if (
                    not self._prepare()
                ):  # the repository is still missing: the failure was just reported
                    continue
                reply, result = self.lead.ask(text)
                self.log.log(
                    "message", "chat", role="assistant", text=reply, error=result.error
                )
            except Exception as e:
                self.log.log(
                    "message",
                    "chat",
                    role="assistant",
                    text=f"Something went wrong: {e}",
                    error=str(e),
                )
            finally:
                self.busy = False
                self.log.log("chat_status", "chat", busy=False)

    def _intro(self, info: dict[str, Any], ask: bool = True) -> None:
        scope = (
            "the last commit"
            if self.scope == "range"
            else "the whole repository (it has a single commit)"
        )
        exts = ", ".join(
            [
                f"{k} {v}"
                for k, v in info.get("extensions", {}).items()
                if k != "(none)"
            ][:4]
        )
        memory = (
            " I also have notes from earlier reviews in `.scr_agents/`."
            if self.memory and self.memory.exists
            else ""
        )
        text = (
            f"Attached **{self.title}** ({info['branch']} @ `{info['head'][:10]}`, "
            f"{info['files']} files: {exts}). By default I review {scope}.{memory}"
        )
        if ask:  # a first message is already waiting: no need to ask what to check
            text += (
                "\n\nWhat should I check? For example: "
                "*review the last 3 commits for injection bugs*, "
                "*audit the whole project for auth problems*, "
                "or *what does the scheduling service do?*"
            )
        self.log.log("message", "chat", role="assistant", text=text, intro=True)

    def _restore(self, previous: list[dict[str, Any]]) -> None:
        for r in previous:
            if r.get("event") == "message" and not r.get("intro"):
                self.lead.history.append(
                    (r.get("role") or "user", str(r.get("text") or ""))
                )
            elif r.get("event") == "run_started" and r.get("run"):
                self.runs.append(r["run"])
            elif r.get("event") == "run_done" and r.get("status") == "finished":
                d = self.manager.run_dir(r["run"])
                if (d / "report.json").exists():
                    self.last_report = EvidenceReport.model_validate_json(
                        (d / "report.json").read_text()
                    )
                    self.last_run_dir = d

    def summary(self) -> dict[str, Any]:
        """Return the chat's entry for the console's conversation list."""
        first = next(
            (
                r
                for r in self.feed.records
                if r.get("event") == "message" and r.get("role") == "user"
            ),
            None,
        )
        return {
            "chat_id": self.chat_id,
            "title": self.title,
            "repo": str(self.repo or ""),
            "target": self.target,
            "first_message": (first or {}).get("text"),
            "scope": self.scope,
            "base": self.base,
            "head": self.head,
            "busy": self.busy,
            "live": True,
            "runs": list(self.runs),
            "mode": self.mode,
            "orchestrator": self.orchestrator,
            "preparing": not self.ready.is_set() and self.prepare_error is None,
            "prepare_error": self.prepare_error,
            "created_at": session_meta(self.settings, self.chat_id).get(
                "created_at", ""
            ),
        }


class ChatManager:
    """The web console's conversations, loaded in memory or stored on disk."""

    def __init__(self, settings: Settings, manager: RunManager):
        """Keep the settings and the run manager every chat starts its reviews with."""
        self.settings = settings
        self.manager = manager
        self.chats: dict[str, WebChat] = {}
        self._lock = threading.Lock()

    def create(
        self,
        target: str,
        repo: Path | None = None,
        first_message: str | None = None,
        orchestrator: str | None = None,
    ) -> WebChat:
        """Start a new chat about ``target`` (see ``WebChat``) and keep it loaded."""
        chat = WebChat(
            self.settings,
            self.manager,
            repo,
            target,
            first_message=first_message,
            orchestrator=orchestrator,
        )
        with self._lock:
            self.chats[chat.chat_id] = chat
        return chat

    def get(self, chat_id: str, restore: bool = True) -> WebChat | None:
        """Return a chat, restoring it from its session when it is not loaded.

        Args:
            chat_id: The chat's session id.
            restore: Whether to load a chat that is not in memory; ``False`` only
                looks at the loaded chats.

        Returns:
            The chat, or ``None`` if there is no such web chat (or it is a demo
            chat whose repository is gone).
        """
        with self._lock:
            chat = self.chats.get(chat_id)
        if chat is not None or not restore:
            return chat
        meta = session_meta(self.settings, chat_id)
        repo, target = (
            meta.get("repo") or "",
            meta.get("target") or meta.get("repo") or "",
        )
        if (
            meta.get("kind") != CHAT_KIND
            or not target
            or (target == "demo" and not Path(repo).exists())
        ):
            return None
        # a missing checkout is re-cloned from the target on the chat's thread
        chat = WebChat(
            self.settings,
            self.manager,
            Path(repo) if repo and Path(repo).exists() else None,
            target,
            chat_id=chat_id,
        )
        with self._lock:
            self.chats.setdefault(chat_id, chat)
            return self.chats[chat_id]

    def delete(self, chat_id: str) -> dict[str, Any]:
        """Delete a conversation and the reviews it started.

        Refused while it is still working.
        """
        from scr_agents.sessions import DeleteError, delete_session

        with self._lock:
            chat = self.chats.get(chat_id)
        if self.records(chat_id) is None and chat is None:
            raise DeleteError(f"no such conversation: {chat_id}")
        if chat is not None:
            running = [
                r
                for r in chat.runs
                if (live := self.manager.get(r)) and live.status == "running"
            ]
            if chat.busy or running:
                what = (
                    "still getting the repository"
                    if not chat.ready.is_set() and not chat.prepare_error
                    else "still working"
                )
                raise ChatBusyError(
                    f"the conversation is {what}; wait for it to finish, then delete it"
                )
            with self._lock:
                self.chats.pop(chat_id, None)
            chat.log.close()
        for run_id in chat.runs if chat else []:
            self.manager.forget(run_id)
        return delete_session(self.settings, chat_id)

    def records(self, chat_id: str) -> list[dict[str, Any]] | None:
        """Transcript of a chat that is not loaded, straight from disk."""
        if session_meta(self.settings, chat_id).get("kind") != CHAT_KIND:
            return None
        return _read_log(session_dir(self.settings, chat_id) / "activity.jsonl")

    def list(self) -> list[dict[str, Any]]:
        """Return the entries of every conversation, loaded or on disk, newest first."""
        out = {cid: c.summary() for cid, c in self.chats.items()}
        for s in list_sessions(self.settings):
            if s.get("kind") != CHAT_KIND or s["session_id"] in out:
                continue
            recs = self.records(s["session_id"]) or []
            first = next(
                (
                    r
                    for r in recs
                    if r.get("event") == "message" and r.get("role") == "user"
                ),
                None,
            )
            title = (
                project_name(s["target"]).rsplit("-", 1)[0]
                if s.get("target") and is_url(s["target"])
                else Path(s.get("repo") or s.get("target") or "?").name
            )
            out[s["session_id"]] = {
                "chat_id": s["session_id"],
                "title": title,
                "repo": s.get("repo"),
                "target": s.get("target"),
                "first_message": (first or {}).get("text"),
                "busy": False,
                "live": False,
                # what the chat runs its reviews with once it resumes
                "orchestrator": s.get("orchestrator")
                or self.settings.review.orchestrator,
                "runs": s.get("runs", []),
                "created_at": s.get("created_at", ""),
            }
        return sorted(
            out.values(), key=lambda c: c.get("created_at") or "", reverse=True
        )
