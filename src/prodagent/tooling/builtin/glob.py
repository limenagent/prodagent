from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from prodagent.core.error_reason import ErrorReason
from prodagent.core.types import ErrorSeverity, SideEffectLevel, ToolError, ToolMeta
from prodagent.tooling.builtin._atomic import check_path
from prodagent.tooling.builtin._fs_common import _DEFAULT_IGNORE_DIRS, normalize_allowed_dirs
from prodagent.tooling.decorator import tool

if TYPE_CHECKING:
    from collections.abc import Iterator

    from prodagent.tooling.base import FunctionTool

logger = logging.getLogger(__name__)

__all__ = ["make_glob"]

_MAX_RESULTS = 1000

_GLOB_DESCRIPTION = (
    "Fast file-pattern matching — returns paths matching a glob pattern. "
    "Honors common ignore dirs (.git, node_modules, __pycache__, etc.). "
    "Use this to find files by name or extension; use grep for content search."
)


def make_glob(allowed_dirs: list[Path] | None = None) -> FunctionTool:
    allowed = normalize_allowed_dirs(allowed_dirs)

    async def _glob(
        pattern: Annotated[
            str,
            Field(
                description=(
                    "Glob pattern to match files. Supports ** for recursive "
                    "matching (e.g. 'src/**/*.py'). Use braces for alternation "
                    "(e.g. '*.{ts,tsx}')."
                )
            ),
        ],
        path: Annotated[
            str, Field(description="Directory to search from. Defaults to current directory.")
        ] = ".",
    ) -> dict[str, Any] | ToolError:
        base = Path(path).expanduser()
        if allowed:
            resolved = check_path(base, allowed)
            if resolved is None:
                return ToolError.from_reason(
                    ErrorReason.FORMAT_ERROR,
                    code="path_not_allowed",
                    message=f"Path '{path}' is outside the allowed directories.",
                    hint="Use a path under one of the allowed directories.",
                )
            base = resolved

        if not base.exists():
            return ToolError.from_reason(
                ErrorReason.FORMAT_ERROR,
                code="path_not_found",
                message=f"Search path '{path}' does not exist.",
                hint="Check the path and retry.",
            )
        if not base.is_dir():
            return ToolError.from_reason(
                ErrorReason.FORMAT_ERROR,
                code="not_a_directory",
                message=f"'{path}' is not a directory.",
                hint="glob requires a directory to search.",
            )

        matches: list[str] = []
        try:
            for entry in _walk(base, pattern):
                matches.append(str(entry))
                if len(matches) >= _MAX_RESULTS:
                    break
        except OSError as exc:
            return ToolError.from_reason(
                ErrorReason.UNKNOWN,
                code="glob_error",
                message=f"Error walking '{path}': {exc}",
                hint="Check permissions and retry.",
                severity=ErrorSeverity.YELLOW,
            )

        return {
            "matches": matches,
            "count": len(matches),
            "truncated": len(matches) >= _MAX_RESULTS,
            "pattern": pattern,
            "path": str(base),
            "ok": True,
        }

    meta = ToolMeta(
        name="glob",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        domain="fs:read",
        estimated_latency_ms=500,
    )
    return tool(_glob, name="glob", description=_GLOB_DESCRIPTION, meta=meta)


def _walk(root: Path, pattern: str) -> Iterator[Path]:
    # Path.glob would descend into ignored dirs; filter at walk time so node_modules subtrees never enumerate.
    pattern = pattern.lstrip("/")

    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            continue

        # yield matches at this level (non-recursive part of the pattern)
        try:
            for match in current.glob(pattern):
                if match.is_dir():
                    continue
                if _is_ignored(match):
                    continue
                yield match
        except (ValueError, OSError):
            pass

        # recurse into subdirs (skipping ignored ones)
        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.name in _DEFAULT_IGNORE_DIRS:
                continue
            stack.append(entry)


def _is_ignored(path: Path) -> bool:
    return any(part in _DEFAULT_IGNORE_DIRS for part in path.parts)
