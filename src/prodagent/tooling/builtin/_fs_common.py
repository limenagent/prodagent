"""Shared filesystem constants + helpers for builtin tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
    """Canonical form of the configured allowlist."""
    if not allowed_dirs:
        return []
    return [d.expanduser().resolve(strict=False) for d in allowed_dirs]
