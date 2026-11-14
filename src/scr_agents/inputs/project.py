"""Project resolution: a local path or a git URL.

URLs are cloned under settings.projects_dir.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from scr_agents.inputs import git as g

log = logging.getLogger("scr_agents")

_URL = re.compile(r"^(?:https?://|git@|ssh://|git://|file://)")
_GH_SHORT = re.compile(r"^[\w.-]+/[\w.-]+$")


def is_url(target: str) -> bool:
    """Tell whether ``target`` names a repository to clone rather than a local path.

    An ``owner/repo`` shorthand counts as a URL only when no such local path exists.
    """
    if _URL.match(target):
        return True
    return bool(_GH_SHORT.match(target)) and not Path(target).expanduser().exists()


def project_name(url: str) -> str:
    """Cache directory name: the repository name plus a hash of the whole URL.

    The hash means `alice/utils` and `bob/utils` never share a clone.
    """
    name = url.rstrip("/").split("/")[-1].split(":")[-1]
    name = name[:-4] if name.endswith(".git") else name or "project"
    return f"{name}-{hashlib.sha1(url.rstrip('/').encode()).hexdigest()[:8]}"


def resolve_project(target: str, projects_dir: Path, update: bool = True) -> Path:
    """Return a local git checkout for `target` (path, URL or owner/repo shorthand)."""
    if not is_url(target):
        p = Path(target).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"project path not found: {p}")
        if not g.is_repo(p):
            raise g.GitError(f"{p} is not inside a git repository")
        return Path(g.git(p, "rev-parse", "--show-toplevel").strip())
    url = target if _URL.match(target) else f"https://github.com/{target}.git"
    dest = Path(projects_dir).resolve() / project_name(url)
    # One clone per destination at a time: a second request for the same URL waits for
    # the first instead of finding a half-written directory.
    with _clone_lock(dest):
        if _complete_clone(dest):
            if update:
                _update_clone(dest)
            return dest
        # left behind by an interrupted clone: it is our cache, never a user directory
        if dest.exists():
            log.warning("replacing incomplete clone %s", dest)
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Clone next to the destination and rename into place, so `dest` only ever holds
        # a finished clone (also against another process cloning the same URL).
        tmp = dest.with_name(f".{dest.name}.partial-{uuid.uuid4().hex[:8]}")
        try:
            g.git_cmd("clone", "--quiet", "--", url, str(tmp), timeout=1800)
        except g.GitError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            raise g.GitError(f"git clone {url} failed: {e}") from e
        try:
            tmp.rename(dest)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            if not _complete_clone(
                dest
            ):  # someone else finished first, unless what they left is broken
                raise
        return dest


_CLONE_LOCKS: dict[str, threading.Lock] = {}
_CLONE_LOCKS_GUARD = threading.Lock()


def _clone_lock(dest: Path) -> threading.Lock:
    with _CLONE_LOCKS_GUARD:
        return _CLONE_LOCKS.setdefault(str(dest), threading.Lock())


def _complete_clone(dest: Path) -> bool:
    """A clone is usable once it has a commit checked out.

    A bare .git from an interrupted clone is not.
    """
    if not dest.is_dir() or not g.is_repo(dest):
        return False
    try:
        g.resolve(dest, "HEAD")
    except g.GitError:
        return False
    return True


def _update_clone(dest: Path) -> None:
    """Fetch and move the cached clone's HEAD to the remote's default branch.

    That way `HEAD~1..HEAD` means the newest commits rather than whatever was current
    when the cache was created.
    """
    try:
        g.git(dest, "fetch", "--prune", "--quiet", "origin", timeout=600)
    except g.GitError as e:
        log.warning("fetch of %s failed, reviewing the cached state: %s", dest, e)
        return
    remote_head = g.git(
        dest, "symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False
    ).strip()
    if not remote_head:
        g.git(dest, "remote", "set-head", "origin", "--auto", check=False)
        remote_head = g.git(
            dest, "symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False
        ).strip()
    if remote_head:
        g.git(dest, "checkout", "-q", "--detach", remote_head, check=False)


def default_scope(repo: Path) -> tuple[str, str, str]:
    """Pick the default review scope as ``(base, head, scope)``.

    The last commit when there is a parent, else the whole tree.
    """
    try:
        g.resolve(repo, "HEAD~1")
        return "HEAD~1", "HEAD", "range"
    except g.GitError:
        return "HEAD", "HEAD", "all"


def describe_repo(repo: Path) -> dict[str, Any]:
    """Summarise a repository for the chat views.

    Returns:
        A JSON-serialisable dict with the path, HEAD commit, branch, number of tracked
        files, the eight most common file extensions and the five latest commits.
    """
    head = g.resolve(repo, "HEAD")
    branch = (
        g.git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip() or "?"
    )
    files = g.git(repo, "ls-files").splitlines()
    exts: dict[str, int] = {}
    for f in files:
        ext = Path(f).suffix.lower() or "(none)"
        exts[ext] = exts.get(ext, 0) + 1
    top = sorted(exts.items(), key=lambda kv: -kv[1])[:8]
    return {
        "path": str(repo),
        "head": head,
        "branch": branch,
        "files": len(files),
        "extensions": dict(top),
        "last_commits": [c.model_dump() for c in g.recent_commits(repo, "HEAD", 5)],
    }
