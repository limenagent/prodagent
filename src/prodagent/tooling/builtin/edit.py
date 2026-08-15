from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from prodagent.core.error_reason import ErrorReason
from prodagent.core.types import ErrorSeverity, SideEffectLevel, ToolError, ToolMeta
from prodagent.tooling.builtin._atomic import atomic_write_text, check_path
from prodagent.tooling.builtin._fs_common import normalize_allowed_dirs
from prodagent.tooling.decorator import tool

if TYPE_CHECKING:
    from prodagent.tooling.base import FunctionTool

logger = logging.getLogger(__name__)

__all__ = ["make_edit"]

_EDIT_DESCRIPTION = (
    "Edit a file by exact-string replacement. The file MUST have been read "
    "first (read-before-edit gate). The old_string must match exactly (no "
    "regex) and appear exactly once — include surrounding context lines to "
    "make it unique. Set replace_all=true only to replace every occurrence."
)


def make_edit(
    seen_paths: set[Path],
    allowed_dirs: list[Path] | None = None,
) -> FunctionTool:
    allowed = normalize_allowed_dirs(allowed_dirs)
    path_locks: dict[str, asyncio.Lock] = {}

    async def _edit(
        file_path: Annotated[
            str, Field(description="Path to the file to edit. Must have been read first.")
        ],
        old_string: Annotated[
            str,
            Field(
                description=(
                    "The exact text to replace. Must appear exactly once in the file "
                    "unless replace_all is true. Include surrounding context to make it unique."
                )
            ),
        ],
        new_string: Annotated[str, Field(description="The replacement text.")],
        replace_all: Annotated[
            bool,
            Field(
                description="Replace all occurrences of old_string. Use only when you intend to replace every match."
            ),
        ] = False,
    ) -> dict[str, Any] | ToolError:
        if old_string == "":
            return ToolError.from_reason(
                ErrorReason.FORMAT_ERROR,
                code="empty_old_string",
                message="old_string must not be empty.",
                hint="Provide the exact text to replace, including surrounding context.",
            )

        path = Path(file_path).expanduser()
        resolved = check_path(path, allowed)
        if resolved is None:
            return ToolError.from_reason(
                ErrorReason.FORMAT_ERROR,
                code="path_not_allowed",
                message=f"Path '{file_path}' is outside allowed directories.",
                hint="Use a path under one of the allowed directories.",
            )
        path = resolved

        lock = path_locks.setdefault(str(path), asyncio.Lock())
        async with lock:
            # read-before-edit gate — check-then-use is atomic under the per-path lock
            if path not in seen_paths:
                return ToolError.from_reason(
                    ErrorReason.FORMAT_ERROR,
                    code="read_before_edit_required",
                    message=f"File '{file_path}' has not been read in this session.",
                    hint="Call read() on it first before editing.",
                )

            if not path.exists():
                seen_paths.discard(path)
                return ToolError.from_reason(
                    ErrorReason.FORMAT_ERROR,
                    code="file_not_found",
                    message=f"File {file_path!r} not found.",
                    hint="Check the path and retry.",
                )

            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return ToolError.from_reason(
                    ErrorReason.UNKNOWN,
                    code="file_read_error",
                    message=f"Error reading file: {exc}",
                    hint="Check file permissions and retry.",
                    severity=ErrorSeverity.YELLOW,
                )

            count = content.count(old_string)
            if count == 0:
                return ToolError.from_reason(
                    ErrorReason.FORMAT_ERROR,
                    code="old_string_not_found",
                    message=f"old_string not found in '{file_path}'.",
                    hint="Make sure it matches exactly (whitespace, indentation).",
                )
            if count > 1 and not replace_all:
                return ToolError.from_reason(
                    ErrorReason.FORMAT_ERROR,
                    code="old_string_not_unique",
                    message=f"old_string appears {count} times in '{file_path}'.",
                    hint="Add more surrounding context to make it unique, or set replace_all=true.",
                )

            if replace_all:
                new_content = content.replace(old_string, new_string)
                replacements = count
            else:
                new_content = content.replace(old_string, new_string, 1)
                replacements = 1

            try:
                atomic_write_text(path, new_content)
            except OSError as exc:
                return ToolError.from_reason(
                    ErrorReason.UNKNOWN,
                    code="file_write_error",
                    message=f"Error writing file: {exc}",
                    hint="Check file permissions and disk space, then retry.",
                    severity=ErrorSeverity.YELLOW,
                )

            seen_paths.add(path)
            return {
                "content": f"Edited {path.name}: {replacements} replacement(s) made.",
                "path": str(path),
                "replacements": replacements,
                "ok": True,
            }

    meta = ToolMeta(
        name="edit",
        is_readonly=False,
        side_effect_level=SideEffectLevel.MEDIUM,
        domain="fs:edit",
        estimated_latency_ms=1_000,
    )
    return tool(_edit, name="edit", description=_EDIT_DESCRIPTION, meta=meta)
