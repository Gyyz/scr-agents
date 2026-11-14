"""File extension to language mapping."""

from __future__ import annotations

from pathlib import PurePosixPath

EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".go": "go",
    ".rs": "rust",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".swift": "swift",
    ".sh": "shell",
    ".bash": "shell",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".md": "markdown",
    ".rst": "markdown",
    ".txt": "text",
}

DOC_LANGS = {"markdown", "text"}
CODE_LANGS = {
    "python",
    "c",
    "cpp",
    "go",
    "rust",
    "javascript",
    "typescript",
    "java",
    "kotlin",
    "ruby",
    "php",
    "csharp",
    "swift",
    "shell",
}


def detect_language(path: str) -> str | None:
    """Map a path to a language name by its extension, or None when it is unknown."""
    return EXT_LANG.get(PurePosixPath(path).suffix.lower())
