"""Activity log: JSONL, one record per event, tagged with run id and commit ids."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scr_agents.models import now_iso


def _jsonable(obj: Any) -> Any:
    """Make a trace value JSON-serialisable.

    SDK objects (pydantic models) in a trace are dumped as JSON, anything else as text.
    """
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json", exclude_none=True)
        except TypeError:
            return dump()
    return str(obj)


class ActivityLog:
    """Append-only JSONL record of a run or a chat session, shared by its agents.

    Thread-safe, so agents running in parallel can share one instance. The web
    console and the terminal view render these records.
    """

    def __init__(
        self,
        path: Path,
        run_id: str,
        commit_ids: list[str],
        subscribers: list[Callable[[dict[str, Any]], None]] | None = None,
    ):
        """Open ``path`` for appending, creating its directory if needed.

        Args:
            path: The ``activity.jsonl`` file to append to.
            run_id: Run (or session) id stamped on every record.
            commit_ids: Commits under review, stamped on every record.
            subscribers: Callbacks that receive each record as it is written.
        """
        self.path = Path(path)
        self.run_id = run_id
        self.commit_ids = list(commit_ids)
        self._lock = threading.Lock()
        self._subscribers = list(subscribers or [])
        self._seq = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Receive every record as it is written (used by the live web UI)."""
        with self._lock:
            self._subscribers.append(callback)

    def log(self, event: str, agent: str | None = None, **payload: Any) -> None:
        """Append one event record and pass it to every subscriber.

        The record carries ``seq``, ``ts``, ``run_id``, ``commit_ids``, ``event``
        and ``agent``, followed by the payload fields. A subscriber that raises
        is skipped; it never stops the run.

        Args:
            event: Event name, such as ``"tool_call"`` or ``"stage"``.
            agent: Name of the agent that produced the event, if any.
            **payload: Event-specific fields; values JSON cannot encode are
                written to the file as text.
        """
        with self._lock:
            self._seq += 1
            rec = {
                "seq": self._seq,
                "ts": now_iso(),
                "run_id": self.run_id,
                "commit_ids": self.commit_ids,
                "event": event,
                "agent": agent,
                **payload,
            }
            self._fh.write(json.dumps(rec, default=str) + "\n")
            self._fh.flush()
            subscribers = list(self._subscribers)
        for cb in subscribers:
            try:
                cb(rec)
            except Exception:
                continue

    def write_trace(self, name: str, data: dict[str, Any]) -> str | None:
        """Store the full record of one model or tool call next to the log.

        The file is `trace/<n>-<name>.json`. Returns the path relative to the log's
        directory (what events carry in `trace`), or None if it could not be written.
        """
        trace_dir = self.path.parent / "trace"
        with self._lock:
            self._trace_n = getattr(self, "_trace_n", 0) + 1
            n = self._trace_n
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:80]
        fname = f"{n:05d}-{slug}.json"
        try:
            trace_dir.mkdir(parents=True, exist_ok=True)
            (trace_dir / fname).write_text(
                json.dumps(data, default=_jsonable, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except OSError:
            return None
        return f"trace/{fname}"

    def close(self) -> None:
        """Close the log file; calling it again does nothing."""
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    @staticmethod
    def read(path: Path) -> list[dict[str, Any]]:
        """Load every record of an ``activity.jsonl`` file, in the order written."""
        out = []
        for line in Path(path).read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out


class NullLog(ActivityLog):
    """Discards everything; handy in unit tests."""

    def __init__(self) -> None:
        """Set the attributes callers read; no file is opened."""
        self.run_id = "none"
        self.commit_ids = []
        self._lock = threading.Lock()

    def log(self, event: str, agent: str | None = None, **payload: Any) -> None:
        """Discard the event."""
        return None

    def write_trace(self, name: str, data: dict[str, Any]) -> str | None:
        """Discard the trace and return ``None``: there is no trace file."""
        return None

    def close(self) -> None:
        """Do nothing: there is no file to close."""
        return None
