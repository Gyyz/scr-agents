"""Sphinx configuration for the project documentation.

Project name, version and authors are read from ``pyproject.toml`` so that
renaming the project needs no edits in this file.
"""

import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
with _pyproject.open("rb") as _f:
    _project_table = tomllib.load(_f)["project"]

# Console entry points declared as "module:function" under [project.scripts].
# They are commands, not library API, so the reference leaves them out.
_entry_points: frozenset[str] = frozenset(
    target.replace(":", ".") for target in _project_table.get("scripts", {}).values()
)

project: str = _project_table["name"]
author: str = ", ".join(a["name"] for a in _project_table.get("authors", []))
copyright: str = f"{datetime.now(tz=UTC).year}, {author}"
release: str = _project_table["version"]
version: str = release

extensions: list[str] = ["myst_parser", "sphinx.ext.napoleon", "autoapi.extension"]
exclude_patterns: list[str] = ["_build"]

myst_heading_anchors: int = 3

napoleon_google_docstring: bool = True
napoleon_numpy_docstring: bool = False

# Parsed statically from src/, so the package is never imported and never
# needs to be named here: renaming the project leaves this file untouched.
autoapi_dirs: list[str] = ["../src"]
autoapi_options: list[str] = [
    "members",
    "undoc-members",
    "show-inheritance",
    "show-module-summary",
]
autoapi_python_class_content: str = "both"
autoapi_member_order: str = "groupwise"

html_theme: str = "furo"
html_title: str = project


def _skip_entry_points(
    app: Any, what: str, name: str, obj: Any, skip: bool, options: Any
) -> bool | None:
    """Hide console entry points from the generated API reference.

    Args:
        app: The Sphinx application.
        what: The kind of object, such as ``"function"`` or ``"class"``.
        name: The fully qualified name of the object.
        obj: The AutoAPI object being documented.
        skip: Whether AutoAPI already intends to skip the object.
        options: The AutoAPI options in effect.

    Returns:
        ``True`` to skip an entry point, otherwise ``None`` to leave AutoAPI's
        own decision in place.
    """
    return True if name in _entry_points else None


def setup(app: Any) -> None:
    """Register the AutoAPI member filter with Sphinx.

    Args:
        app: The Sphinx application.
    """
    app.connect("autoapi-skip-member", _skip_entry_points)
