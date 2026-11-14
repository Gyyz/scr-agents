"""Builds a small demo repository: a clean base commit and a defective head commit.

The head commit introduces a command injection, unsafe deserialization, a C buffer
overflow and a logic regression.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

BASE_UTILS = '''"""Small helpers."""
import subprocess


def run(cmd: list[str]) -> str:
    """Run a command given as an argv list."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def last_n(items: list, n: int) -> list:
    """Return the last n items."""
    if n <= 0:
        return []
    return items[-n:]
'''

HEAD_UTILS = (
    '''"""Small helpers."""
import pickle
import subprocess


def run(cmd: str) -> str:
    """Run a shell command string (convenient for callers)."""
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, '''
    '''check=False).stdout


def load_settings(blob: bytes):
    """Load settings sent by the client."""
    return pickle.loads(blob)


def last_n(items: list, n: int) -> list:
    """Return the last n items."""
    if n <= 0:
        return []
    return items[-n - 1:]
'''
)

BASE_TEST = """from app.utils import last_n, run


def test_last_n():
    assert last_n([1, 2, 3, 4], 2) == [3, 4]
    assert last_n([1], 0) == []


def test_run_echo():
    assert "hi" in run(["echo", "hi"])
"""

HEAD_TEST = """from app.utils import last_n, run


def test_last_n():
    assert last_n([1, 2, 3, 4], 2) == [3, 4]
    assert last_n([1], 0) == []


def test_run_echo():
    assert "hi" in run("echo hi")
"""

HEAD_C = """#include <stdio.h>
#include <string.h>

/* Copies the client-supplied name into a fixed buffer. */
int greet(const char *name) {
    char buf[32];
    strcpy(buf, name);
    printf("hello %s\\n", buf);
    return 0;
}

int main(int argc, char **argv) {
    if (argc > 1) {
        return greet(argv[1]);
    }
    return 0;
}
"""


def _git(repo: Path, *args: str) -> str:
    cmd = [
        "git",
        "-C",
        str(repo),
        "-c",
        "user.name=scr-agents-demo",
        "-c",
        "user.email=demo@example.com",
        "-c",
        "commit.gpgsign=false",
        *args,
    ]
    return subprocess.run(
        cmd, check=True, capture_output=True, text=True
    ).stdout.strip()


DEMO_MARKER = (
    ".scr-agents-demo"  # lets `scr-agents demo --dir` recognise a directory it created
)


def make_demo_repo(dest: Path) -> tuple[str, str]:
    """Create the demo repo at `dest`; returns (base_sha, head_sha)."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / DEMO_MARKER).write_text("created by `scr-agents demo`; safe to delete\n")
    _git(dest, "init", "-q", "-b", "main")
    (dest / "app").mkdir()
    (dest / "tests").mkdir()
    (dest / "app" / "__init__.py").write_text("")
    (dest / "app" / "utils.py").write_text(BASE_UTILS)
    (dest / "tests" / "test_utils.py").write_text(BASE_TEST)
    (dest / "README.md").write_text("# demo\n")
    (dest / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n\n'
        '[tool.pytest.ini_options]\npythonpath = ["."]\n'
    )
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "Initial helpers")
    base = _git(dest, "rev-parse", "HEAD")

    (dest / "app" / "utils.py").write_text(HEAD_UTILS)
    (dest / "tests" / "test_utils.py").write_text(HEAD_TEST)
    (dest / "native").mkdir()
    (dest / "native" / "greet.c").write_text(HEAD_C)
    _git(dest, "add", "-A")
    _git(
        dest,
        "commit",
        "-q",
        "-m",
        "Accept shell strings in run(), add settings loader and native greeter",
        "-m",
        "Callers kept building strings anyway, so run() now takes a string.\n"
        "Also adds a tiny C greeter for the embedded build.",
    )
    head = _git(dest, "rev-parse", "HEAD")
    return base, head
