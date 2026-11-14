"""Sandboxed shell: argv-only execution of allowlisted binaries.

It adds per-binary argument policies, paths confined to the sandbox root, a throwaway
HOME, a scrubbed env, clamped timeouts with process-group kill, and output caps.

What this does NOT do: isolate the network or the filesystem for code the reviewed
project runs itself (its tests, Makefile recipes, build scripts). Running those is the
point of the test bench; containing them needs a container or VM around the whole
process (see deploy/).
"""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Any

from scr_agents.config import ShellConfig


def ensure_tool_path() -> str:
    """Put the running interpreter's ``bin`` folder first on ``PATH``, once.

    Scanners (ruff, bandit, semgrep) and test runners (pytest) are installed next to
    the interpreter. Started as ``.venv/bin/scr-agents`` from a service or a launcher,
    the virtual environment is not on ``PATH``, so they would count as not installed
    and ``python3 -m pytest`` would run a Python without pytest.

    Returns:
        The ``PATH`` now in effect.
    """
    bin_dir = str(Path(sys.executable).parent)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if bin_dir not in parts:
        os.environ["PATH"] = os.pathsep.join([bin_dir, *(p for p in parts if p)])
    return os.environ["PATH"]


class ShellPolicyError(PermissionError):
    """Raised when the sandbox refuses a command; the message says what was blocked."""

    pass


@dataclass
class ShellResult:
    """Outcome of one sandboxed command.

    ``exit_code`` is ``None`` when the command could not start or timed out, and
    ``error`` then says why. ``truncated`` means stdout or stderr was cut to the cap.
    """

    argv: list[str]
    cwd: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    truncated: bool = False
    timed_out: bool = False
    error: str | None = None

    @property
    def command(self) -> str:
        """The command line, shell-quoted (for display only; no shell ever runs it)."""
        return shlex.join(self.argv)

    def to_dict(self) -> dict[str, Any]:
        """Return the fields as a dict, plus the quoted ``command``."""
        d = asdict(self)
        d["command"] = self.command
        return d


class SandboxedShell:
    """Argv-only runner for allowlisted binaries, confined to one sandbox root."""

    def __init__(
        self, config: ShellConfig, root: Path, allow_local_executables: bool = False
    ):
        """Set up a shell confined to ``root``.

        Args:
            config: Allowlist, argument policies, environment, timeout and output cap.
            root: The sandbox root. Working directories must be inside it, and so
                must path arguments unless they are under ``allowed_path_prefixes``.
                Its ``.home`` folder is the throwaway HOME.
            allow_local_executables: Also run an executable given by a path inside
                ``root``, such as a binary the project just built.
        """
        self.config = config
        self.root = Path(root).resolve()
        self.home = (
            self.root / ".home"
        )  # throwaway HOME: no ~/.ssh, ~/.gitconfig, ~/.npmrc of the host user
        self.allow_local_executables = allow_local_executables

    # ------------------------------------------------------------------ policy
    def _check(self, argv: list[str], cwd: str | Path | None) -> Path:
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ShellPolicyError("argv must be a non-empty list of strings")
        exe = argv[0]
        if "/" in exe or exe.startswith("."):
            target = (Path(cwd) if cwd else self.root) / exe
            if not self.allow_local_executables or not _inside(
                target.resolve(), self.root
            ):
                raise ShellPolicyError(
                    f"executable path '{exe}' not allowed "
                    "(set shell.allow_local_executables)"
                )
        elif exe not in self.config.allowed_binaries:
            raise ShellPolicyError(f"binary '{exe}' not in shell.allowed_binaries")
        wd = Path(cwd).resolve() if cwd else self.root
        if not _inside(wd, self.root):
            raise ShellPolicyError(
                f"cwd '{wd}' is outside the sandbox root {self.root}"
            )
        if not wd.is_dir():
            raise ShellPolicyError(f"cwd '{wd}' does not exist")
        self._check_args(os.path.basename(exe), argv[1:], wd)
        return wd

    def _check_args(self, binary: str, args: list[str], wd: Path) -> None:
        denied = set(self.config.denied_args.get(binary, []))
        for a in args:
            if a in denied or a.split("=", 1)[0] in denied:
                raise ShellPolicyError(
                    f"'{binary} {a}' is not allowed in the sandbox (shell.denied_args)"
                )
        allowed_sub = self.config.allowed_subcommands.get(binary)
        if allowed_sub is not None:
            first = next((a for a in args if not a.startswith("-")), None)
            if first is not None and first not in allowed_sub:
                raise ShellPolicyError(
                    f"'{binary} {first}' is not allowed; permitted subcommands: "
                    f"{', '.join(allowed_sub)}"
                )
        if binary in ("python", "python3"):
            for i, a in enumerate(args):
                module = (
                    args[i + 1]
                    if a == "-m" and i + 1 < len(args)
                    else a[2:]
                    if a.startswith("-m") and len(a) > 2
                    else None
                )
                if module is not None and module not in self.config.python_modules:
                    why = (
                        "; packages cannot be installed in the offline sandbox, "
                        "use the standard library"
                        if module in ("pip", "pip3", "ensurepip")
                        else ""
                    )
                    raise ShellPolicyError(
                        f"'python -m {module}' is not allowed; permitted modules: "
                        f"{', '.join(self.config.python_modules)}{why}"
                    )
        for a in args:
            for candidate in _path_candidates(a):
                self._check_path(candidate, wd)

    def _check_path(self, candidate: str, wd: Path) -> None:
        if candidate.startswith("~"):
            raise ShellPolicyError(f"path '{candidate}' refers to a home directory")
        if candidate.startswith("/"):
            p = Path(candidate).resolve()
            if _inside(p, self.root):
                return
            for prefix in self.config.allowed_path_prefixes:
                if _inside(p, Path(prefix)):
                    return
            raise ShellPolicyError(
                f"path '{candidate}' is outside the sandbox root "
                "and the allowed prefixes"
            )
        if ".." in candidate.split("/"):
            p = (wd / candidate).resolve()
            if not _inside(p, self.root):
                raise ShellPolicyError(f"path '{candidate}' escapes the sandbox root")

    # ------------------------------------------------------------------ execution
    def _env(self) -> dict[str, str]:
        env = {k: os.environ[k] for k in self.config.env_passthrough if k in os.environ}
        home = str(self.home)
        env.update(
            {
                "HOME": home,
                "XDG_CONFIG_HOME": f"{home}/.config",
                "XDG_CACHE_HOME": f"{home}/.cache",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "NPM_CONFIG_USERCONFIG": "/dev/null",
                "PYTHONDONTWRITEBYTECODE": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "CI": "1",
                "NO_COLOR": "1",
                "SCR_AGENTS_SANDBOX": "1",
            }
        )
        return env

    def run(
        self,
        argv: list[str],
        cwd: str | Path | None = None,
        timeout: int | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> ShellResult:
        """Run ``argv`` in the sandbox and capture its output.

        Args:
            argv: The command as a list; ``argv[0]`` must be an allowlisted binary
                (or a local executable, when those are allowed).
            cwd: Working directory inside the sandbox root; defaults to the root.
            timeout: Seconds before the process group is killed, capped at
                ``shell.timeout_s``.
            env_extra: Variables set on top of the scrubbed environment.

        Returns:
            The result. A missing executable or a timeout is reported in it, not
            raised.

        Raises:
            ShellPolicyError: If the policy refuses the command, an argument or
                ``cwd``.
        """
        wd = self._check(argv, cwd)
        cap = self.config.max_output_chars
        limit = (
            min(int(timeout), self.config.timeout_s)
            if timeout
            else self.config.timeout_s
        )
        self.home.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(wd),
                    env={**self._env(), **(env_extra or {})},
                    stdout=out_f,
                    stderr=err_f,
                    stdin=subprocess.DEVNULL,
                    shell=False,
                    start_new_session=True,
                )
            except FileNotFoundError as e:
                return ShellResult(
                    argv=list(argv),
                    cwd=str(wd),
                    exit_code=None,
                    stdout="",
                    stderr="",
                    duration_s=round(time.monotonic() - t0, 3),
                    error=f"executable not found: {e.filename}",
                )
            timed_out, error = False, None
            try:
                code: int | None = proc.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                code, timed_out, error = None, True, f"timed out after {limit}s"
            out, out_cut = _read_capped(out_f, cap)
            err, err_cut = _read_capped(err_f, cap)
        return ShellResult(
            argv=list(argv),
            cwd=str(wd),
            exit_code=code,
            stdout=out,
            stderr=err,
            duration_s=round(time.monotonic() - t0, 3),
            truncated=out_cut or err_cut,
            timed_out=timed_out,
            error=error,
        )


def _path_candidates(arg: str) -> list[str]:
    """Return the parts of an argument that may name a path.

    That is the whole value, the part after '=', and the tail of an attached flag such
    as ``-I/usr/include`` or ``-o../x``.
    """
    out = [arg]
    if "=" in arg:
        out.append(arg.split("=", 1)[1])
    if arg.startswith("-"):
        for i, ch in enumerate(arg):
            if ch in "/~" or arg.startswith("..", i):
                out.append(arg[i:])
                break
    return out


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill the child and everything it spawned.

    They share the session started by Popen.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _read_capped(f: IO[bytes], cap: int) -> tuple[str, bool]:
    """Read at most ``cap`` characters of a temp file the child wrote to.

    It never buffers the whole output. Returns the text and whether it was cut.
    """
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)
    text = f.read(cap + 1).decode("utf-8", errors="replace")
    return text[:cap], size > cap or len(text) > cap


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
