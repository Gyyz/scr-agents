"""Sessions: every run lives under out/sessions/<session_id>/<run_id>/.

A session is one interaction: a `scr-agents chat` conversation, one `scr-agents serve`
process, or a single `scr-agents review` / `demo` invocation. The session directory
holds `session.json` (kind, created_at, metadata), the session's own `activity.jsonl`
for chat sessions, and one directory per run (request.json, activity.jsonl,
report.md/json, human_review.md).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, cast

from scr_agents.config import Settings
from scr_agents.models import new_id, now_iso

LEGACY_SESSION = "legacy"


def sessions_root(settings: Settings) -> Path:
    """Return the absolute ``<output_dir>/sessions`` directory holding all sessions."""
    return Path(settings.output_dir).resolve() / "sessions"


def session_dir(settings: Settings, session_id: str) -> Path:
    """Return the directory of ``session_id``; it may not exist yet."""
    return sessions_root(settings) / session_id


def ensure_session(
    settings: Settings, session_id: str, kind: str = "cli", **meta: Any
) -> Path:
    """Create the session directory and its session.json if they do not exist yet."""
    d = session_dir(settings, session_id)
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "session.json"
    if not marker.exists():
        marker.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "kind": kind,
                    "created_at": now_iso(),
                    **meta,
                },
                indent=2,
                default=str,
            )
            + "\n"
        )
    return d


def new_session(settings: Settings, kind: str, **meta: Any) -> str:
    """Create a session with a fresh id and return the id.

    ``kind`` says what started it (``cli``, ``demo``, ``web``, a chat ...); any other
    keyword arguments are stored in its ``session.json``.
    """
    session_id = new_id("session")
    ensure_session(settings, session_id, kind, **meta)
    return session_id


def session_meta(settings: Settings, session_id: str) -> dict[str, Any]:
    """Return the contents of the session's ``session.json``.

    A missing file, or one that is not valid JSON, yields a stub whose ``kind`` is
    ``unknown``.
    """
    marker = session_dir(settings, session_id) / "session.json"
    if not marker.exists():
        return {"session_id": session_id, "kind": "unknown", "created_at": ""}
    try:
        return cast("dict[str, Any]", json.loads(marker.read_text()))
    except json.JSONDecodeError:
        return {"session_id": session_id, "kind": "unknown", "created_at": ""}


def run_dir(settings: Settings, session_id: str, run_id: str) -> Path:
    """Return the directory of run ``run_id`` in session ``session_id``."""
    return session_dir(settings, session_id) / run_id


def iter_run_dirs(settings: Settings) -> list[tuple[str, Path]]:
    """All ``(session_id, run_dir)`` pairs: session runs first, then legacy runs.

    Legacy runs are top-level ``out/run-*`` directories, paired with the ``legacy``
    session id.
    """
    out: list[tuple[str, Path]] = []
    root = sessions_root(settings)
    if root.exists():
        for s in sorted(root.iterdir()):
            if s.is_dir():
                for r in sorted(s.iterdir()):
                    if r.is_dir() and (r / "request.json").exists():
                        out.append((s.name, r))
    top = Path(settings.output_dir).resolve()
    if top.exists():
        for r in sorted(top.iterdir()):
            if (
                r.is_dir()
                and r.name.startswith("run-")
                and (r / "request.json").exists()
            ):
                out.append((LEGACY_SESSION, r))
    return out


def find_run_dir(settings: Settings, run_id: str) -> Path | None:
    """Return the directory of ``run_id`` in any session (legacy too), or ``None``."""
    for _sid, d in iter_run_dirs(settings):
        if d.name == run_id:
            return d
    return None


def list_sessions(settings: Settings) -> list[dict[str, Any]]:
    """Sessions with their run ids, newest first."""
    runs_by_session: dict[str, list[str]] = {}
    for sid, d in iter_run_dirs(settings):
        runs_by_session.setdefault(sid, []).append(d.name)
    root = sessions_root(settings)
    ids = set(runs_by_session)
    if root.exists():
        ids |= {s.name for s in root.iterdir() if s.is_dir()}
    sessions = []
    for sid in ids:
        meta = (
            session_meta(settings, sid)
            if sid != LEGACY_SESSION or (root / sid).exists()
            else {"session_id": sid, "kind": "legacy", "created_at": ""}
        )
        sessions.append(
            {**meta, "session_id": sid, "runs": runs_by_session.get(sid, [])}
        )
    sessions.sort(key=lambda s: s.get("created_at") or "", reverse=True)
    return sessions


def tidy_legacy(settings: Settings) -> list[str]:
    """Move top-level ``out/run-*`` directories into ``out/sessions/legacy/``.

    Returns:
        The ids of the runs moved. A run whose name already exists there stays
        where it is.
    """
    top = Path(settings.output_dir).resolve()
    moved: list[str] = []
    if not top.exists():
        return moved
    legacy = [r for r in top.iterdir() if r.is_dir() and r.name.startswith("run-")]
    if not legacy:
        return moved
    dest = ensure_session(
        settings,
        LEGACY_SESSION,
        kind="legacy",
        note="runs created before sessions existed",
    )
    for r in sorted(legacy):
        target = dest / r.name
        if target.exists():
            continue
        shutil.move(str(r), str(target))
        moved.append(r.name)
    return moved


# ---------------------------------------------------------------- deletion
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class DeleteError(ValueError):
    """A refused delete: bad or unknown id, or a target outside the sessions root."""

    pass


def _inside_sessions(settings: Settings, path: Path) -> Path:
    root = sessions_root(settings).resolve()
    target = path.resolve()
    if target == root or root not in target.parents:
        raise DeleteError(f"refusing to delete {target}: not inside {root}")
    return target


def delete_session(settings: Settings, session_id: str) -> dict[str, Any]:
    """Remove a session directory: its transcript, traces and every run in it.

    Returns:
        The ``session_id`` and the ids of the runs that were removed.

    Raises:
        DeleteError: If ``session_id`` is malformed, unknown or resolves outside
            the sessions root.
    """
    if not _ID.match(session_id or ""):
        raise DeleteError(f"not a session id: {session_id!r}")
    d = session_dir(settings, session_id)
    if not d.is_dir():
        raise DeleteError(f"no such session: {session_id}")
    target = _inside_sessions(settings, d)
    runs = sorted(
        r.name for r in target.iterdir() if r.is_dir() and (r / "request.json").exists()
    )
    shutil.rmtree(target)
    return {"session_id": session_id, "runs": runs}


def delete_run(settings: Settings, run_id: str) -> dict[str, Any]:
    """Remove one run directory.

    A session left with nothing but its session.json is removed as well.

    Returns:
        The ``run_id``, its ``session_id`` and ``removed_empty_session``: the
        session's name if it was removed too, else ``None``.

    Raises:
        DeleteError: If ``run_id`` is malformed or unknown, or the run is not
            inside the sessions root (a legacy top-level run).
    """
    if not _ID.match(run_id or "") or not run_id.startswith("run-"):
        raise DeleteError(f"not a run id: {run_id!r}")
    d = find_run_dir(settings, run_id)
    if d is None:
        raise DeleteError(f"no such run: {run_id}")
    target = _inside_sessions(settings, d)
    session = target.parent
    shutil.rmtree(target)
    removed_session = None
    leftovers = (
        [x for x in session.iterdir() if x.name != "session.json"]
        if session.is_dir()
        else []
    )
    if (
        session.is_dir()
        and not leftovers
        and session.resolve() != sessions_root(settings).resolve()
    ):
        shutil.rmtree(session)
        removed_session = session.name
    return {
        "run_id": run_id,
        "session_id": session.name,
        "removed_empty_session": removed_session,
    }
