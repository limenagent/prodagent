"""Shared filesystem constants + helpers for builtin tools."""

from __future__ import annotations

from pathlib import Path

_DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        "dist",
        "build",
        ".eggs",
        ".cache",
    }
)


def normalize_allowed_dirs(allowed_dirs: list[Path] | None) -> list[Path]:
    """Canonical form of the configured allowlist.

    No allowlist configured (None/empty) collapses to the current working
    directory — least privilege by default; pass an explicit list to widen.
    """
    if not allowed_dirs:
        return [Path.cwd().resolve(strict=False)]
    return [d.expanduser().resolve(strict=False) for d in allowed_dirs]
