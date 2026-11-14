"""Build the ReviewRequest from a repo, two refs and an optional PR.

The ReviewRequest is the shared input format.

scope="range" reviews the diff between base and head (the normal PR-style review);
scope="all" reviews the whole repository at head, presenting every code file as added.
"""

from __future__ import annotations

from pathlib import Path

from scr_agents.inputs import git as g
from scr_agents.inputs.languages import CODE_LANGS, detect_language
from scr_agents.inputs.pr import load_pr
from scr_agents.models import ChangedFile, Hunk, ReviewRequest, Task, split_lines
from scr_agents.ws2_analysis.code_graph import SKIP_DIRS

MAX_WHOLE_FILE_BYTES = 200_000


def build_request(
    repo: str | Path,
    base: str,
    head: str = "HEAD",
    pr: str | None = None,
    task: Task | str = Task.FIND_BUGS,
    claim: str | None = None,
    instructions: str | None = None,
    scope: str = "range",
    max_files: int = 400,
) -> ReviewRequest:
    """Resolve the refs and collect the files, commits and PR text to review.

    Args:
        repo: The repository's directory.
        base: Base ref; with ``scope="range"`` the review covers ``base..head``.
        head: Head ref to review.
        pr: Optional PR source for ``load_pr``: a JSON file or ``gh:<number>``.
        task: What the reviewers are asked to do.
        claim: Optional claim to check, stored on the request.
        instructions: Optional extra instructions, stored on the request.
        scope: ``"range"`` for the diff between ``base`` and ``head``, ``"all"`` for
            every code file at ``head``.
        max_files: Upper bound on the files collected when ``scope`` is ``"all"``.

    Returns:
        The request, with both refs also resolved to commit SHAs.

    Raises:
        GitError: ``repo`` is not a git repository or a ref does not resolve.
    """
    repo = Path(repo).resolve()
    if not g.is_repo(repo):
        raise g.GitError(f"not a git repository: {repo}")
    base_sha = g.resolve(repo, base)
    head_sha = g.resolve(repo, head)
    if scope == "all":
        files = whole_tree_files(repo, head_sha, max_files)
        commits = g.recent_commits(repo, head_sha, 20)
    else:
        files = g.changed_files(repo, base_sha, head_sha)
        commits = g.commits_between(repo, base_sha, head_sha)
    return ReviewRequest(
        repo_path=str(repo),
        base_ref=base,
        head_ref=head,
        base=base_sha,
        head=head_sha,
        task=Task(task),
        claim=claim,
        instructions=instructions,
        scope="all" if scope == "all" else "range",
        files=files,
        commits=commits,
        pr=load_pr(pr, repo),
    )


def whole_tree_files(repo: Path, head_sha: str, max_files: int) -> list[ChangedFile]:
    """Represent every code file at `head_sha` as an added file.

    That way the diff pipeline reviews it whole.
    """
    out: list[ChangedFile] = []
    for path in g.tree_files(repo, head_sha):
        if any(part in SKIP_DIRS for part in Path(path).parts):
            continue
        lang = detect_language(path)
        if lang not in CODE_LANGS:
            continue
        try:
            text = g.show_file(repo, head_sha, path)
        except g.GitError:
            continue
        if len(text.encode("utf-8", errors="replace")) > MAX_WHOLE_FILE_BYTES:
            continue
        lines = split_lines(text)
        hunk = Hunk(
            old_start=0,
            old_lines=0,
            new_start=1,
            new_lines=len(lines),
            text="".join("+" + line + "\n" for line in lines),
        )
        out.append(
            ChangedFile(
                path=path, status="added", language=lang, hunks=[hunk], added=len(lines)
            )
        )
        if len(out) >= max_files:
            break
    return out
