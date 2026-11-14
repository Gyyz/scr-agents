"""Workspace: base and head commits exported as plain directories in the run directory.

They are `git archive` exports, not worktrees: the checkouts carry no `.git`, so code
built or run in them cannot reach the repository, its hooks or its config.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Self

from scr_agents.inputs import git as g
from scr_agents.models import ReviewRequest


class Workspace:
    """Base and head checkouts of a review, present while the ``with`` block runs."""

    def __init__(self, request: ReviewRequest, run_dir: Path):
        """Set up the checkout paths; nothing is exported before ``__enter__``.

        Args:
            request: The review whose repository and commits are checked out.
            run_dir: The run directory; the checkouts go under its ``worktrees``
                folder.
        """
        self.request = request
        self.root = Path(run_dir) / "worktrees"
        self.base_dir = self.root / "base"
        self.head_dir = self.root / "head"

    def __enter__(self) -> Self:
        """Export both commits, removing what was exported if either export fails."""
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            g.export_commit(self.request.repo_path, self.request.base, self.base_dir)
            g.export_commit(self.request.repo_path, self.request.head, self.head_dir)
        except Exception:
            shutil.rmtree(self.root, ignore_errors=True)
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        """Delete both checkouts, whether or not the block raised."""
        shutil.rmtree(self.root, ignore_errors=True)

    def dir_for(self, ref: str) -> Path:
        """Return the checkout directory of ``ref``.

        Args:
            ref: ``base`` or ``head``, or the commit SHA or ref name of either.

        Raises:
            ValueError: If ``ref`` names neither commit.
        """
        if ref in ("head", self.request.head, self.request.head_ref):
            return self.head_dir
        if ref in ("base", self.request.base, self.request.base_ref):
            return self.base_dir
        raise ValueError(f"unknown ref '{ref}', use 'base' or 'head'")
