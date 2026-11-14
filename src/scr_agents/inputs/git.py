"""Thin git wrapper (subprocess, no third-party dependency) plus a unified-diff parser.

Every host-side git call runs with repository hooks, fsmonitor and external diff
drivers disabled and a scrubbed environment: the repositories handled here are
untrusted, and anything planted in their `.git/` must not execute on the host.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tarfile
from pathlib import Path

from scr_agents.inputs.languages import detect_language
from scr_agents.models import ChangedFile, Commit, Hunk, split_lines

log = logging.getLogger("scr_agents")

_LOG_FORMAT = "%H%x1f%an%x1f%aI%x1f%s%x1f%b%x1e"
# what host git may see from our environment: enough for PATH, ssh agents, proxies and
# the user's own global config (credential helpers); never API keys or other service
# secrets
HOST_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "GIT_SSH_COMMAND",
    "XDG_CONFIG_HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
)
# config overrides for every call: no hooks, no fsmonitor daemon, literal (unquoted)
# non-ASCII paths
HARDENING = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.quotepath=false",
)


class GitError(RuntimeError):
    """A git command failed, timed out, or the target is not a usable repository."""

    pass


def host_env() -> dict[str, str]:
    """Build the environment for host git: the ``HOST_ENV_KEYS`` subset, no prompts.

    Terminal prompts are off so a missing credential fails instead of hanging, and the
    system-wide git config is ignored.
    """
    env = {k: os.environ[k] for k in HOST_ENV_KEYS if k in os.environ}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"})
    return env


def git_cmd(
    *args: str, cwd: str | Path | None = None, check: bool = True, timeout: int = 120
) -> str:
    """Run git with the hardening overrides; `args` come after them."""
    try:
        proc = subprocess.run(
            ["git", *HARDENING, *args],
            cwd=str(cwd) if cwd else None,
            env=host_env(),
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(args[:3])} timed out after {timeout}s") from e
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def git(repo: str | Path, *args: str, check: bool = True, timeout: int = 120) -> str:
    """Run a hardened git command in ``repo`` (``git -C repo ...``) and return stdout.

    Raises:
        GitError: The command failed (when ``check`` is set) or timed out.
    """
    return git_cmd("-C", str(repo), *args, check=check, timeout=timeout)


def is_repo(path: str | Path) -> bool:
    """Tell whether ``path`` is inside a git work tree."""
    try:
        return git(path, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except GitError:
        return False


def resolve(repo: str | Path, ref: str) -> str:
    """Resolve ``ref`` to the full SHA of the commit it points to.

    Raises:
        GitError: ``ref`` does not name a commit.
    """
    return git(
        repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"
    ).strip()


def short(sha: str) -> str:
    """Abbreviate a commit SHA to 12 characters for display."""
    return sha[:12]


_DIFF_HEADER = re.compile(r'^diff --git ("?)a/(.*?)\1 ("?)b/(.*?)\3$')
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
_C_ESCAPE = re.compile(r"\\(?:([0-7]{3})|(.))")
_C_SIMPLE = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    '"': '"',
    "\\": "\\",
}


def unquote_path(name: str, quoted: bool) -> str:
    """Undo git's C-style quoting of paths with spaces/control characters.

    Octal escapes are UTF-8 bytes.
    """
    if not quoted:
        return name
    out = bytearray()
    pos = 0
    for m in _C_ESCAPE.finditer(name):
        out += name[pos : m.start()].encode("utf-8")
        out += (
            bytes([int(m.group(1), 8)])
            if m.group(1)
            else _C_SIMPLE.get(m.group(2), m.group(2)).encode("utf-8")
        )
        pos = m.end()
    out += name[pos:].encode("utf-8")
    return out.decode("utf-8", errors="replace")


def parse_unified_diff(text: str) -> list[ChangedFile]:
    """Parse ``git diff`` output into one ``ChangedFile`` per file, with its hunks.

    Records the status (added, deleted, renamed, binary), the old path of a rename and
    the added and removed line counts.
    """
    files: list[ChangedFile] = []
    cur: ChangedFile | None = None
    hunk: Hunk | None = None
    for line in split_lines(
        text
    ):  # not splitlines(): form feeds and other separators are file content
        m = _DIFF_HEADER.match(line)
        if m:
            old, new = (
                unquote_path(m.group(2), bool(m.group(1))),
                unquote_path(m.group(4), bool(m.group(3))),
            )
            cur = ChangedFile(
                path=new,
                old_path=old if old != new else None,
                language=detect_language(new),
            )
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if hunk is None:
            # file header section
            if line.startswith("new file mode"):
                cur.status = "added"
            elif line.startswith("deleted file mode"):
                cur.status = "deleted"
            elif line.startswith("rename from "):
                cur.old_path = _bare_path(line[len("rename from ") :])
                cur.status = "renamed"
            elif line.startswith("rename to "):
                cur.path = _bare_path(line[len("rename to ") :])
                cur.language = detect_language(cur.path)
            elif line.startswith("Binary files"):
                cur.status = "binary"
        hm = _HUNK_HEADER.match(line)
        if hm:
            hunk = Hunk(
                old_start=int(hm.group(1)),
                old_lines=int(hm.group(2) or 1),
                new_start=int(hm.group(3)),
                new_lines=int(hm.group(4) or 1),
                header=hm.group(5).strip(),
            )
            cur.hunks.append(hunk)
            continue
        if hunk is not None:
            hunk.text += line + "\n"
            if line.startswith("+"):
                cur.added += 1
            elif line.startswith("-"):
                cur.removed += 1
    return files


def _bare_path(name: str) -> str:
    quoted = len(name) >= 2 and name[0] == name[-1] == '"'
    return unquote_path(name[1:-1] if quoted else name, quoted)


def changed_files(repo: str | Path, base: str, head: str) -> list[ChangedFile]:
    """Diff ``base`` against ``head`` (with rename detection) and parse the result."""
    # --no-ext-diff: a diff.external planted in the repository's .git/config must
    # not run
    text = git(
        repo, "diff", "--no-ext-diff", "-U3", "-M", "--no-color", base, head, "--"
    )
    return parse_unified_diff(text)


def _parse_log(out: str) -> list[Commit]:
    commits: list[Commit] = []
    for rec in out.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) >= 5 and parts[0].strip():
            commits.append(
                Commit(
                    sha=parts[0].strip(),
                    author=parts[1],
                    date=parts[2],
                    subject=parts[3],
                    body=parts[4].strip(),
                )
            )
    return commits


def commits_between(repo: str | Path, base: str, head: str) -> list[Commit]:
    """List the commits in ``base..head``, oldest first."""
    return _parse_log(
        git(repo, "log", "--reverse", f"--format={_LOG_FORMAT}", f"{base}..{head}")
    )


def recent_commits(repo: str | Path, ref: str = "HEAD", n: int = 20) -> list[Commit]:
    """List the ``n`` latest commits reachable from ``ref``, newest first."""
    return _parse_log(git(repo, "log", f"-n{n}", f"--format={_LOG_FORMAT}", ref))


def log_for_path(
    repo: str | Path, path: str, n: int = 10, ref: str = "HEAD"
) -> list[Commit]:
    """List the ``n`` latest commits reachable from ``ref`` that touch ``path``."""
    return _parse_log(
        git(repo, "log", f"-n{n}", f"--format={_LOG_FORMAT}", ref, "--", path)
    )


def tree_files(repo: str | Path, ref: str = "HEAD") -> list[str]:
    """List every file path in the tree of ``ref``, recursively."""
    return [
        p for p in git(repo, "ls-tree", "-r", "--name-only", "-z", ref).split("\0") if p
    ]


def show_file(repo: str | Path, ref: str, path: str) -> str:
    """Return the contents of ``path`` as of ``ref``.

    Raises:
        GitError: ``path`` does not exist at ``ref``.
    """
    return git(repo, "show", f"{ref}:{path}")


def export_commit(repo: str | Path, sha: str, dest: str | Path) -> Path:
    """Materialise a commit as plain files under `dest`.

    There is no `.git`, so nothing executed in that tree can reach the repository, its
    hooks or its config. Symlinks that point outside `dest` are skipped.
    """
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ["git", *HARDENING, "-C", str(repo), "archive", "--format=tar", sha],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=host_env(),
        stdin=subprocess.DEVNULL,
    )
    skipped = 0
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                try:
                    tar.extract(member, dest, filter="data")
                except tarfile.FilterError:
                    skipped += 1  # absolute or escaping links, device files
    finally:
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        proc.wait(timeout=60)
    if proc.returncode != 0:
        raise GitError(f"git archive {sha[:12]} failed: {err.strip()}")
    if skipped:
        log.warning(
            "export of %s: skipped %d entr%s that would escape the checkout",
            sha[:12],
            skipped,
            "y" if skipped == 1 else "ies",
        )
    return dest
