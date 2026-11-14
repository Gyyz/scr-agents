"""Fixes for problems seen in real-model runs.

Node/TS bench detection, proof-of-concept hints, the pip refusal message.
"""

import json
import shutil

import pytest

from scr_agents.config import ShellConfig
from scr_agents.ws1_review.tools import run_hint, run_with
from scr_agents.ws3_harness.shell import SandboxedShell, ShellPolicyError
from scr_agents.ws3_harness.test_bench import detect_commands, missing_dependencies


def _pkg(d, scripts=None, deps=None):
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.json").write_text(
        json.dumps({"name": "x", "scripts": scripts or {}, "dependencies": deps or {}})
    )
    return d


def test_node_project_without_tests_gets_its_build_script(tmp_path):
    """A Node project without a real test script gets its build script run instead."""
    # the pet-store layout: build is `tsc`, there is no test script -> the bench builds
    # instead of skipping
    d = _pkg(tmp_path / "a", {"build": "tsc", "start": "node dist/index.js"})
    assert detect_commands(d) == (["npm", "run", "--silent", "build"], None)
    d = _pkg(tmp_path / "b", {"test": "jest", "build": "tsc"})
    assert detect_commands(d) == (
        ["npm", "run", "--silent", "build"],
        ["npm", "test", "--silent"],
    )
    # npm's placeholder test script is not a test suite
    d = _pkg(tmp_path / "c", {"test": 'echo "Error: no test specified" && exit 1'})
    assert detect_commands(d) == (None, None)


def test_typescript_without_build_script_type_checks(tmp_path):
    """TypeScript without a build script is type-checked, once ``tsc`` is installed."""
    d = _pkg(tmp_path / "ts")
    (d / "tsconfig.json").write_text("{}")
    assert detect_commands(d) == (None, None)  # no compiler available
    (d / "node_modules" / "typescript" / "bin").mkdir(parents=True)
    (d / "node_modules" / "typescript" / "bin" / "tsc").write_text("")
    assert detect_commands(d)[0] == [
        "node",
        "node_modules/typescript/bin/tsc",
        "--noEmit",
        "-p",
        ".",
    ]


def test_missing_node_dependencies_skip_instead_of_failing(tmp_path):
    """Uninstalled Node dependencies give a skip reason; anything else gives none."""
    d = _pkg(tmp_path / "deps", {"build": "tsc"}, {"express": "^4"})
    reason = missing_dependencies(d, ["npm", "run", "build"])
    assert reason and "node_modules" in reason and "offline" in reason
    (d / "node_modules").mkdir()
    assert missing_dependencies(d, ["npm", "run", "build"]) is None
    assert (
        missing_dependencies(
            _pkg(tmp_path / "nodeps", {"build": "x"}), ["npm", "run", "build"]
        )
        is None
    )
    assert missing_dependencies(d, ["python3", "-m", "pytest"]) is None


@pytest.mark.skipif(not shutil.which("npm"), reason="npm not installed")
def test_bench_runs_a_node_build(demo_repo, tmp_path):
    """A Node build passing on base and failing on head is reported as a regression."""
    from scr_agents.config import BenchConfig
    from scr_agents.inputs.request import build_request
    from scr_agents.ws3_harness.test_bench import TestBench
    from scr_agents.ws3_harness.workspace import Workspace

    repo, base, head = demo_repo
    with Workspace(build_request(repo, base, head), tmp_path / "run") as ws:
        for d in (ws.base_dir, ws.head_dir):
            for f in ("pyproject.toml",):
                (d / f).unlink(missing_ok=True)
            import shutil as sh

            sh.rmtree(d / "tests", ignore_errors=True)
            _pkg(d, {"build": "node build.js"})
            (d / "build.js").write_text("console.log('built ok')\n")
        (ws.head_dir / "build.js").write_text("process.exit(3)\n")
        report = TestBench(
            BenchConfig(), ShellConfig(allowed_binaries=["npm", "node"]), ws
        ).run_both()
    assert report.base.passed and "built ok" in report.base.stdout
    assert not report.head.passed and report.head.exit_code == 3
    assert report.delta == "regression: base passes, head fails"


def test_run_with_picks_the_interpreter():
    """A proof-of-concept script is run with the interpreter its extension calls for."""
    assert run_with(".scr_agents_scratch/F-1/poc.py") == [
        "python3",
        ".scr_agents_scratch/F-1/poc.py",
    ]
    assert run_with("x/poc.js") == ["node", "x/poc.js"]
    assert run_with("x/poc.ts") == ["node", "-r", "ts-node/register", "x/poc.ts"]
    assert run_with("x/input.bin") is None


def test_run_hints_for_the_failures_seen_in_real_runs(tmp_path):
    """Common proof-of-concept setup failures get a hint; other errors get none."""
    root = tmp_path
    (root / "src" / "services").mkdir(parents=True)
    (root / "src" / "services" / "ComplianceService.ts").write_text(
        "export class C {}\n"
    )
    (root / "app").mkdir()
    argv = ["python3", ".scr_agents_scratch/F-1/poc.py"]
    # 1. `requests` is not installed and cannot be
    h = run_hint(argv, "ModuleNotFoundError: No module named 'requests'", root)
    assert h and "third-party" in h and "urllib.request" in h
    # 2. a Python script importing a TypeScript module
    h = run_hint(
        argv,
        "ModuleNotFoundError: No module named 'src.services.ComplianceService'",
        root,
    )
    assert h and "TypeScript" in h and "node" in h
    # 3. a local Python package: the path is already importable
    assert "PYTHONPATH" in run_hint(
        argv, "ModuleNotFoundError: No module named 'app.utils'", root
    )
    # 4. node: relative require from the scratch directory, missing package without
    # node_modules
    h = run_hint(["node", "x.js"], "Error: Cannot find module '../src/routes'", root)
    assert h and "NODE_PATH" in h
    h = run_hint(["node", "x.js"], "Error: Cannot find module 'supertest'", root)
    assert h and "built-in" in h
    # 5. running a script that was never written
    h = run_hint(
        argv,
        (
            "python3: can't open file '/w/.scr_agents_scratch/F-1/poc.py': "
            "[Errno 2] No such file"
        ),
        root,
    )
    assert h and "write_file" in h
    assert run_hint(argv, "Traceback ... ValueError: boom", root) is None


def test_pip_is_refused_with_a_reason(tmp_path):
    """``pip`` is refused with an offline-sandbox reason other refusals do not give."""
    sh = SandboxedShell(ShellConfig(allowed_binaries=["python3"]), tmp_path)
    with pytest.raises(ShellPolicyError, match="offline sandbox"):
        sh.run(["python3", "-m", "pip", "install", "requests"])
    with pytest.raises(ShellPolicyError) as e:
        sh.run(["python3", "-m", "http.server"])
    assert "offline" not in str(e.value)
