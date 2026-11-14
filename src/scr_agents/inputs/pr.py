"""PR and comment loading. Sources: a JSON file, or `gh:<number>` via the GitHub CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from scr_agents.models import PRComment, PullRequest


def load_pr(source: str | None, repo: str | Path | None = None) -> PullRequest | None:
    """Load a PR and its comments from ``source``, or return None when there is none.

    Args:
        source: A PR JSON file, ``gh:<number>`` to fetch it with the GitHub CLI, or
            empty for no PR.
        repo: Repository directory ``gh`` runs in, so it finds the right remote.

    Raises:
        FileNotFoundError: ``source`` is neither ``gh:<number>`` nor an existing file.
        RuntimeError: The GitHub CLI is missing or ``gh pr view`` failed.
    """
    if not source:
        return None
    if source.startswith("gh:"):
        return _load_from_gh(source[3:], repo)
    path = Path(source)
    if path.exists():
        return _from_dict(json.loads(path.read_text()))
    raise FileNotFoundError(f"PR source not found: {source}")


def _from_dict(d: dict[str, Any]) -> PullRequest:
    comments = []
    for c in d.get("comments", []) or []:
        author = c.get("author")
        if isinstance(author, dict):
            author = author.get("login", "")
        comments.append(
            PRComment(
                author=author or "",
                body=c.get("body", ""),
                path=c.get("path"),
                line=c.get("line"),
            )
        )
    author = d.get("author")
    if isinstance(author, dict):
        author = author.get("login", "")
    return PullRequest(
        number=d.get("number"),
        title=d.get("title", ""),
        body=d.get("body", "") or "",
        author=author or "",
        url=d.get("url"),
        comments=comments,
    )


def _load_from_gh(number: str, repo: str | Path | None) -> PullRequest:
    if not shutil.which("gh"):
        raise RuntimeError("gh CLI not installed; pass a PR JSON file instead")
    cmd = [
        "gh",
        "pr",
        "view",
        number,
        "--json",
        "number,title,body,author,comments,url,reviews",
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(repo) if repo else None,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr view failed: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    for r in data.get("reviews", []) or []:
        if r.get("body"):
            data.setdefault("comments", []).append(
                {"author": r.get("author"), "body": r["body"]}
            )
    return _from_dict(data)
