"""Run manager: the web console's reviews, live and finished.

Starts reviews on background threads and fans their activity-log events out to live
subscribers; also reads finished runs back from the output directory.
"""

from __future__ import annotations

import json
import queue
import tempfile
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scr_agents.config import Settings, deep_merge
from scr_agents.inputs.project import resolve_project
from scr_agents.inputs.request import build_request
from scr_agents.models import EvidenceReport, ReviewRequest, now_iso
from scr_agents.pipeline import run_review
from scr_agents.sessions import (
    find_run_dir,
    iter_run_dirs,
    list_sessions,
    new_session,
    session_meta,
)
from scr_agents.ws1_review.rounds import next_request, prepare_round

# one subscriber's records, in order
RecordQueue = queue.Queue[dict[str, Any]]

FINAL_EVENTS = {"run_finished", "run_failed"}


class RunBusyError(RuntimeError):
    """Raised when a run that is still in progress is asked to be deleted."""

    pass


@dataclass
class LiveRun:
    """A review started by this server: its request, its state and every event so far.

    ``status`` is ``"running"``, ``"finished"`` or ``"failed"``. ``errors`` lists
    the agents that failed in a finished run, which makes its report incomplete.
    """

    run_id: str
    request: ReviewRequest
    status: str = "running"  # running | finished | failed
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    counts: dict[str, int] | None = None
    errors: list[str] = field(
        default_factory=list
    )  # agents that failed (report incomplete)
    run_dir: str | None = None
    started_at: str = field(default_factory=now_iso)
    previous_run: str | None = None  # run directory of the round before this one
    _subs: list[RecordQueue] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def emit(self, rec: dict[str, Any]) -> None:
        """Keep an event and put it on every subscriber's queue.

        A record without ``seq`` is numbered after the events already kept.
        """
        with self._lock:
            rec.setdefault("seq", len(self.events) + 1)
            self.events.append(rec)
            subs = list(self._subs)
        for q in subs:
            q.put(rec)

    def subscribe(self) -> tuple[list[dict[str, Any]], RecordQueue]:
        """Return the events so far and a queue that receives every later one.

        Both are taken under one lock, so no event is missed or repeated. Pass the
        queue to ``unsubscribe`` when done.
        """
        with self._lock:
            q: RecordQueue = queue.Queue()
            self._subs.append(q)
            return list(self.events), q

    def unsubscribe(self, q: RecordQueue) -> None:
        """Stop putting events on ``q``; an unknown queue is ignored."""
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def summary(self) -> dict[str, Any]:
        """Return the run's entry for the console's run list, marked ``live``."""
        r = self.request
        return {
            "run_id": self.run_id,
            "status": self.status,
            "repo": r.repo_path,
            "base": r.base,
            "session_id": r.session_id,
            "session_kind": "web",
            "head": r.head,
            "base_ref": r.base_ref,
            "head_ref": r.head_ref,
            "task": r.task.value,
            "claim": r.claim,
            "files": len(r.files),
            "commits": len(r.commits),
            "created_at": r.created_at,
            "counts": self.counts,
            "error": self.error,
            "errors": self.errors,
            "run_dir": self.run_dir,
            "events": len(self.events),
            "live": True,
        }


class RunManager:
    """Start reviews for the web console and find runs, live or on disk."""

    def __init__(self, settings: Settings, session_id: str | None = None):
        """Bind the manager to a session.

        Args:
            settings: The server's settings; each run may override the LLM mode.
            session_id: Session that runs started without one belong to; a new
                ``web`` session is created when it is not given.
        """
        self.settings = settings
        self.session_id = session_id or new_session(settings, "web")
        self.runs: dict[str, LiveRun] = {}
        self._lock = threading.Lock()

    # starting ----------------------------------------------------------------
    def _settings_for(
        self, mode: str | None, orchestrator: str | None = None
    ) -> Settings:
        override: dict[str, Any] = {}
        if mode:
            override["llm"] = {"mode": mode}
        if orchestrator:
            override["review"] = {"orchestrator": orchestrator}
        if not override:
            return self.settings
        return Settings.model_validate(deep_merge(self.settings.model_dump(), override))

    def start(
        self,
        repo: str,
        base: str,
        head: str = "HEAD",
        pr: str | None = None,
        task: str = "find_bugs",
        claim: str | None = None,
        mode: str | None = None,
        instructions: str | None = None,
        scope: str = "range",
        session_id: str | None = None,
        orchestrator: str | None = None,
    ) -> LiveRun:
        """Start a review on a background thread and return without waiting.

        Args:
            repo: Path or git URL of the project; a URL is cloned (or fetched)
                into ``projects_dir`` first.
            base: Base ref of the range to review.
            head: Head ref of the range to review.
            pr: Optional PR source (a JSON file or ``gh:<number>``).
            task: What the reviewers are asked to do.
            claim: Optional claim to check.
            mode: LLM mode for this run only; ``None`` keeps the configured one.
            instructions: Optional extra instructions for the reviewers.
            scope: ``"range"`` for ``base..head``, ``"all"`` for the whole tree.
            session_id: Session to file the run under; defaults to the manager's.
            orchestrator: ``"deterministic"`` or ``"openflow"`` for this run only;
                ``None`` keeps ``review.orchestrator``.

        Returns:
            The run, still ``"running"``; its events arrive through ``subscribe``.

        Raises:
            GitError: If the project cannot be found or cloned, or a ref does not
                resolve.
            FileNotFoundError: If a local project path does not exist.
        """
        settings = self._settings_for(mode, orchestrator)
        repo_path = resolve_project(repo, Path(settings.projects_dir))
        request = build_request(
            repo_path,
            base,
            head,
            pr=pr or None,
            task=task,
            claim=claim or None,
            instructions=instructions or None,
            scope=scope or "range",
            max_files=settings.whole_repo_max_files,
        )
        request.session_id = session_id or self.session_id
        run = LiveRun(run_id=request.run_id, request=request)
        with self._lock:
            self.runs[run.run_id] = run
        threading.Thread(
            target=self._execute,
            args=(settings, run),
            daemon=True,
            name=f"review-{run.run_id}",
        ).start()
        return run

    def start_next_round(self, previous_run: Path, mode: str | None = None) -> LiveRun:
        """Start the next review round of a finished run, in that run's session.

        Args:
            previous_run: Run directory of the round to continue from.
            mode: LLM mode for this run only; ``None`` keeps the configured one.

        Returns:
            The new run, still ``"running"``.

        Raises:
            RoundError: If that round passed or the round limit is reached.
        """
        settings = self._settings_for(mode)
        prepare_round(previous_run, settings.review.max_rounds)  # fail before starting
        previous = EvidenceReport.model_validate_json(
            (previous_run / "report.json").read_text()
        )
        request = next_request(previous)
        request.session_id = request.session_id or self.session_id
        run = LiveRun(
            run_id=request.run_id, request=request, previous_run=str(previous_run)
        )
        with self._lock:
            self.runs[run.run_id] = run
        threading.Thread(
            target=self._execute,
            args=(settings, run),
            daemon=True,
            name=f"review-{run.run_id}",
        ).start()
        return run

    def start_demo(self, mode: str | None = None) -> LiveRun:
        """Create the planted-defect demo repository in a temp folder and review it."""
        from scr_agents.demo import make_demo_repo

        dest = Path(tempfile.mkdtemp(prefix="scr-agents-demo-"))
        base, head = make_demo_repo(dest)
        return self.start(str(dest), base, head, mode=mode)

    def _execute(self, settings: Settings, run: LiveRun) -> None:
        try:
            report, run_dir = run_review(
                settings,
                run.request.repo_path,
                run.request.base_ref,
                run.request.head_ref,
                request=run.request,
                on_event=run.emit,
                session_id=run.request.session_id or self.session_id,
                previous_run=Path(run.previous_run) if run.previous_run else None,
            )
            run.status, run.counts, run.run_dir, run.errors = (
                "finished",
                report.counts(),
                str(run_dir),
                list(report.errors),
            )
            run.emit(
                {
                    "ts": now_iso(),
                    "run_id": run.run_id,
                    "event": "run_finished",
                    "agent": None,
                    "counts": run.counts,
                    "run_dir": run.run_dir,
                    "errors": run.errors,
                }
            )
        except Exception as e:
            run.status, run.error = "failed", f"{type(e).__name__}: {e}"
            run.emit(
                {
                    "ts": now_iso(),
                    "run_id": run.run_id,
                    "event": "run_failed",
                    "agent": None,
                    "error": run.error,
                    "traceback": traceback.format_exc()[-3000:],
                }
            )

    # reading -----------------------------------------------------------------
    def get(self, run_id: str) -> LiveRun | None:
        """Return the run if this server started it, else ``None``."""
        return self.runs.get(run_id)

    def run_dir(self, run_id: str) -> Path:
        """Return the run's directory, or where this session would put a new one."""
        found = find_run_dir(self.settings, run_id)
        return (
            found
            if found is not None
            else Path(self.settings.output_dir).resolve()
            / "sessions"
            / self.session_id
            / run_id
        )

    def disk_summary(self, run_id: str) -> dict[str, Any] | None:
        """Build a run's list entry from its files; ``None`` without ``request.json``.

        A run with a ``report.json`` is finished (its verdicts are counted); one
        without is reported as failed.
        """
        d = self.run_dir(run_id)
        req_path = d / "request.json"
        if not req_path.exists():
            return None
        r = ReviewRequest.model_validate_json(req_path.read_text())
        sid = r.session_id or d.parent.name
        summary = {
            "run_id": run_id,
            "status": "finished" if (d / "report.json").exists() else "failed",
            "session_id": sid,
            "session_kind": session_meta(self.settings, sid).get("kind", "unknown"),
            "repo": r.repo_path,
            "base": r.base,
            "head": r.head,
            "base_ref": r.base_ref,
            "head_ref": r.head_ref,
            "task": r.task.value,
            "claim": r.claim,
            "files": len(r.files),
            "commits": len(r.commits),
            "created_at": r.created_at,
            "counts": None,
            "error": None,
            "run_dir": str(d),
            "events": 0,
            "live": False,
        }
        if (d / "report.json").exists():
            try:
                rep = json.loads((d / "report.json").read_text())
                counts = {"confirmed": 0, "unverified": 0, "rejected": 0}
                for vf in rep.get("findings", []):
                    counts[vf.get("verdict", "unverified")] = (
                        counts.get(vf.get("verdict", "unverified"), 0) + 1
                    )
                summary["counts"] = counts
            except (json.JSONDecodeError, OSError):
                pass
        if (d / "activity.jsonl").exists():
            summary["events"] = sum(
                1
                for line in (d / "activity.jsonl").read_text().splitlines()
                if line.strip()
            )
        return summary

    def summary(self, run_id: str) -> dict[str, Any] | None:
        """Return the list entry of a live or stored run, or ``None`` if unknown."""
        live = self.get(run_id)
        return live.summary() if live else self.disk_summary(run_id)

    def disk_events(self, run_id: str) -> list[dict[str, Any]] | None:
        """Return the events in a run's ``activity.jsonl``, or ``None`` without one."""
        p = self.run_dir(run_id) / "activity.jsonl"
        if not p.exists():
            return None
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    def list_runs(self) -> list[dict[str, Any]]:
        """Return the entries of every run, live and on disk, newest first."""
        out = {rid: run.summary() for rid, run in self.runs.items()}
        for _sid, d in iter_run_dirs(self.settings):
            if d.name not in out:
                s = self.disk_summary(d.name)
                if s:
                    out[d.name] = s
        return sorted(out.values(), key=lambda s: s["created_at"], reverse=True)

    def sessions(self) -> list[dict[str, Any]]:
        """Return the metadata of every session in the output directory."""
        return list_sessions(self.settings)

    def forget(self, run_id: str) -> None:
        """Drop a run from the live runs; its files on disk are kept."""
        with self._lock:
            self.runs.pop(run_id, None)

    def delete(self, run_id: str) -> dict[str, Any]:
        """Delete a finished run from disk (and memory).

        A run still in progress cannot be deleted.
        """
        from scr_agents.sessions import delete_run

        live = self.get(run_id)
        if live is not None and live.status == "running":
            raise RunBusyError(
                f"{run_id} is still running; wait for it to finish, then delete it"
            )
        self.forget(run_id)
        return delete_run(self.settings, run_id)
