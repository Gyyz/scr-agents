"""Test bench: builds and tests both commits.

Only the `local` runner is implemented in v0; `docker` and `qemu` are configuration
placeholders that report a skipped result.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from pathlib import Path

from scr_agents.config import BenchConfig, ShellConfig
from scr_agents.models import BenchReport, BuildResult
from scr_agents.ws3_harness.shell import SandboxedShell, ShellPolicyError, ShellResult
from scr_agents.ws3_harness.workspace import Workspace


def detect_commands(d: Path) -> tuple[list[str] | None, list[str] | None]:
    """Return (build_cmd, test_cmd) guessed from the project layout."""
    if (
        (d / "pyproject.toml").exists()
        or (d / "pytest.ini").exists()
        or (d / "tests").is_dir()
        or any(d.glob("test_*.py"))
    ):
        return None, [
            "python3",
            "-m",
            "pytest",
            "-q",
            "-x",
            "-p",
            "no:cacheprovider",
            "--no-header",
        ]
    if (d / "Cargo.toml").exists():
        return ["cargo", "build", "-q"], ["cargo", "test", "-q"]
    if (d / "go.mod").exists():
        return ["go", "build", "./..."], ["go", "test", "./..."]
    if (d / "package.json").exists():
        scripts = _npm_scripts(d)
        test = ["npm", "test", "--silent"] if "test" in scripts else None
        if "build" in scripts:
            build = ["npm", "run", "--silent", "build"]
        elif (d / "tsconfig.json").exists() and (
            d / "node_modules" / "typescript" / "bin" / "tsc"
        ).exists():
            build = [
                "node",
                "node_modules/typescript/bin/tsc",
                "--noEmit",
                "-p",
                ".",
            ]  # type-check only
        else:
            build = None
        return build, test
    if (d / "CMakeLists.txt").exists():
        return ["cmake", "-S", ".", "-B", "build"], ["cmake", "--build", "build"]
    if (d / "Makefile").exists():
        text = (d / "Makefile").read_text(errors="replace")
        return ["make"], (
            ["make", "test"] if re.search(r"^test\s*:", text, re.MULTILINE) else None
        )
    c_files = sorted(p.relative_to(d).as_posix() for p in d.glob("*.c"))
    if c_files:
        return ["cc", "-Wall", "-Wextra", "-o", "scr_agents_a.out", *c_files], None
    return None, None


def _npm_scripts(d: Path) -> dict[str, str]:
    try:
        data = json.loads((d / "package.json").read_text(errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    # npm's placeholder test script only prints an error; treat it as no tests
    return {
        k: v
        for k, v in (scripts or {}).items()
        if isinstance(v, str) and not (k == "test" and "no test specified" in v)
    }


def missing_dependencies(d: Path, cmd: list[str]) -> str | None:
    """Return why ``cmd`` cannot work offline, or ``None``.

    The sandbox cannot download packages, so a Node project whose dependencies are not
    checked in or pre-installed is reported as skipped, not as failing.
    """
    if cmd[0] not in ("npm", "node") or not (d / "package.json").exists():
        return None
    try:
        data = json.loads((d / "package.json").read_text(errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None
    deps = (
        {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        if isinstance(data, dict)
        else {}
    )
    if deps and not (d / "node_modules").is_dir():
        return (
            "node dependencies are not installed (no node_modules) "
            "and the offline sandbox cannot download them; vendor node_modules "
            "or set bench.build_cmd/test_cmd in .scr_agents/project.yaml"
        )
    return None


def missing_runner(cmd: list[str], res: ShellResult) -> str | None:
    """Return why ``python -m <module>`` could not start, or ``None``.

    A Python without the test runner exits non-zero with ``No module named <module>``.
    That says nothing about the code under review, so the run counts as skipped, not
    as failing tests.
    """
    if res.exit_code in (0, None) or len(cmd) < 3 or cmd[1] != "-m":
        return None
    module = cmd[2]
    if f"No module named {module}" not in f"{res.stderr}\n{res.stdout}":
        return None
    return (
        f"'{module}' is not installed for {cmd[0]}; install it next to scr-agents "
        "or set bench.test_cmd"
    )


class TestBench:
    """Builds and tests the base and head checkouts of a ``Workspace``."""

    def __init__(
        self, config: BenchConfig, shell_config: ShellConfig, workspace: Workspace
    ):
        """Set up a bench over the checkouts of ``workspace``.

        Args:
            config: The runner, the timeout, and build and test commands that replace
                the detected ones.
            shell_config: Policy of the sandboxed shell the commands run in.
            workspace: The base and head checkouts; its root is the sandbox root.
        """
        self.config = config
        self.workspace = workspace
        self.shell = SandboxedShell(shell_config, workspace.root)
        self._lock = (
            threading.Lock()
        )  # validators run in parallel but share the checkouts

    def _result(
        self, ref: str, cmd: list[str] | None, res: ShellResult | None, reason: str = ""
    ) -> BuildResult:
        if res is None:
            return BuildResult(
                ref=ref, command=" ".join(cmd or []), skipped=True, reason=reason
            )
        return BuildResult(
            ref=ref,
            command=res.command,
            exit_code=res.exit_code,
            stdout=res.stdout,
            stderr=res.stderr,
            duration_s=res.duration_s,
            passed=res.exit_code == 0,
            reason=res.error or "",
        )

    def run(self, ref: str, what: str = "test") -> BuildResult:
        """Build or test one checkout, waiting for any run already in progress.

        Args:
            ref: ``base`` or ``head``, or the commit SHA or ref name of either.
            what: ``build`` or ``test``. A project without a test command is built
                instead.

        Returns:
            The result. It is marked skipped when nothing can run: the runner is not
            implemented, no command was detected, its binary or dependencies are
            missing, or the policy refuses it.

        Raises:
            ValueError: If ``ref`` names neither commit.
        """
        with self._lock:
            return self._run(ref, what)

    def _run(self, ref: str, what: str) -> BuildResult:
        d = self.workspace.dir_for(ref)
        if self.config.runner != "local":
            return self._result(
                ref,
                None,
                None,
                f"runner '{self.config.runner}' is not implemented in v0",
            )
        build_cmd, test_cmd = detect_commands(d)
        cmd = (
            (self.config.build_cmd or build_cmd)
            if what == "build"
            else (self.config.test_cmd or test_cmd)
        )
        if what == "test" and cmd is None and (self.config.build_cmd or build_cmd):
            cmd = (
                self.config.build_cmd or build_cmd
            )  # no tests: building is the best signal we have
        if cmd is None:
            return self._result(
                ref, None, None, f"no {what} command detected in {d.name}"
            )
        if not shutil.which(cmd[0]):
            return self._result(ref, cmd, None, f"'{cmd[0]}' is not installed")
        missing = missing_dependencies(d, cmd)
        if missing:
            return self._result(ref, cmd, None, missing)
        try:
            res = self.shell.run(cmd, cwd=d, timeout=self.config.timeout_s)
        except ShellPolicyError as e:
            return self._result(ref, cmd, None, str(e))
        missing = missing_runner(cmd, res)
        if missing:
            return self._result(ref, cmd, None, missing)
        return self._result(ref, cmd, res)

    def run_both(self, what: str = "test") -> BenchReport:
        """Run ``what`` on base and head and describe how the results differ.

        Returns:
            Both results and a ``delta`` such as "regression: base passes, head fails".
        """
        base = self.run("base", what)
        head = self.run("head", what)
        if base.skipped or head.skipped:
            delta = "skipped: " + (base.reason or head.reason)
        elif base.passed and not head.passed:
            delta = "regression: base passes, head fails"
        elif not base.passed and head.passed:
            delta = "fixed: base fails, head passes"
        elif base.passed and head.passed:
            delta = "both pass"
        else:
            delta = "both fail"
        return BenchReport(base=base, head=head, delta=delta)
