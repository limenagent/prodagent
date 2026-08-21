from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from prodagent.core.error_reason import ErrorReason
from prodagent.core.types import ErrorSeverity, SideEffectLevel, ToolError, ToolMeta
from prodagent.tooling.builtin._atomic import atomic_write_text, check_path
from prodagent.tooling.builtin._fs_common import normalize_allowed_dirs
from prodagent.tooling.builtin.edit import make_edit
from prodagent.tooling.builtin.read import make_read
from prodagent.tooling.decorator import tool

if TYPE_CHECKING:
    from prodagent.tooling.base import FunctionTool

logger = logging.getLogger(__name__)

__all__ = ["make_write", "make_builtin_fs_bundle"]

_WRITE_DESCRIPTION = (
    "Create or overwrite a file with the given content. Use edit() for "
    "partial changes; use write() only for new files or complete rewrites."
)


def make_write(allowed_dirs: list[Path] | None = None) -> FunctionTool:
    allowed = normalize_allowed_dirs(allowed_dirs)

    async def _write(
        file_path: Annotated[str, Field(description="Path to the file to create or overwrite.")],
        content: Annotated[str, Field(description="The full content to write to the file.")],
    ) -> dict[str, Any] | ToolError:
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
        try:
            atomic_write_text(path, content)
        except OSError as exc:
            return ToolError.from_reason(
                ErrorReason.UNKNOWN,
                code="file_write_error",
                message=f"Error writing file: {exc}",
                hint="Check file permissions and disk space, then retry.",
                severity=ErrorSeverity.YELLOW,
            )

        return {
            "content": f"Wrote {len(content)} chars to {path.name}.",
            "path": str(path),
            "chars_written": len(content),
            "ok": True,
        }

    meta = ToolMeta(
        name="write",
        is_readonly=False,
        side_effect_level=SideEffectLevel.HIGH,
        domain="fs:write",
        timeout_seconds=30.0,
    )
    return tool(_write, name="write", description=_WRITE_DESCRIPTION, meta=meta)


def make_builtin_fs_bundle(
    allowed_dirs: list[Path] | None = None,
) -> tuple[FunctionTool, FunctionTool, FunctionTool]:
    seen_paths: set[Path] = set()
    allowed = normalize_allowed_dirs(allowed_dirs)
    read_tool = make_read(allowed_dirs=allowed, seen_paths=seen_paths)
    edit_tool = make_edit(seen_paths=seen_paths, allowed_dirs=allowed)
    write_tool = make_write(allowed_dirs=allowed)
    return read_tool, edit_tool, write_tool
