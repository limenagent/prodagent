from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from prodagent.core.error_reason import ErrorReason
from prodagent.core.types import ErrorSeverity, SideEffectLevel, ToolError, ToolMeta
from prodagent.tooling.builtin._atomic import check_path
from prodagent.tooling.builtin._fs_common import normalize_allowed_dirs
from prodagent.tooling.decorator import tool

if TYPE_CHECKING:
    from prodagent.tooling.base import FunctionTool

logger = logging.getLogger(__name__)

__all__ = ["make_read"]

_MAX_LINES_DEFAULT = 2000
_MAX_READ_BYTES = 50 * 1024 * 1024  # 50 MB; larger files must be spilled
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

_READ_DESCRIPTION = (
    "Read a file from the filesystem. Lines are returned with line-number "
    "prefixes (cat -n style) so you can reference exact lines. If the file is "
    "larger than the limit, you'll see a PARTIAL view notice — call again with "
    "a higher offset to read the next chunk. Images are returned as base64; "
    "PDFs and notebooks are handled specially."
)


def make_read(
    allowed_dirs: list[Path] | None = None,
    seen_paths: set[Path] | None = None,
) -> FunctionTool:
    allowed = normalize_allowed_dirs(allowed_dirs)

    async def _read(
        file_path: Annotated[
            str, Field(description="Absolute or relative path to the file to read.")
        ],
        offset: Annotated[
            int, Field(description="Starting line number (0-based). Use for pagination.")
        ] = 0,
        limit: Annotated[
            int, Field(description="Maximum number of lines to return.")
        ] = _MAX_LINES_DEFAULT,
    ) -> dict[str, Any] | ToolError:
        path = Path(file_path).expanduser()
        resolved = check_path(path, allowed)
        if resolved is None:
            return ToolError.from_reason(
                ErrorReason.FORMAT_ERROR,
                code="path_not_allowed",
                message=f"Path '{file_path}' is outside the allowed directories.",
                hint="Use a path under one of the allowed directories.",
            )
        path = resolved

        if not path.exists():
            return ToolError.from_reason(
                ErrorReason.FORMAT_ERROR,
                code="file_not_found",
                message=f"File {file_path!r} not found.",
                hint="Check the path and retry.",
            )
        if not path.is_file():
            return ToolError.from_reason(
                ErrorReason.FORMAT_ERROR,
                code="not_a_file",
                message=f"'{file_path}' is not a regular file.",
                hint="read() only supports regular files.",
            )

        size = path.stat().st_size
        if size > _MAX_READ_BYTES:
            return ToolError.from_reason(
                ErrorReason.PAYLOAD_TOO_LARGE,
                code="file_too_large",
                message=f"File '{file_path}' is {size // (1024 * 1024)} MB — too large to read whole.",
                hint="Use a spill store or read with offset/limit to page through it.",
                severity=ErrorSeverity.RED,
            )

        if path.suffix.lower() in _IMAGE_EXTENSIONS:
            try:
                data = path.read_bytes()
                b64 = base64.b64encode(data).decode("ascii")
                if seen_paths is not None:
                    seen_paths.add(path)
                return {
                    "content": f"data:image/{path.suffix[1:]};base64,{b64}",
                    "path": str(path),
                    "is_image": True,
                    "ok": True,
                }
            except OSError as exc:
                return ToolError.from_reason(
                    ErrorReason.UNKNOWN,
                    code="image_read_error",
                    message=f"Error reading image: {exc}",
                    hint="Check file permissions and retry.",
                    severity=ErrorSeverity.YELLOW,
                )

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolError.from_reason(
                ErrorReason.UNKNOWN,
                code="file_read_error",
                message=f"Error reading file: {exc}",
                hint="Check file permissions and retry.",
                severity=ErrorSeverity.YELLOW,
            )

        if seen_paths is not None:
            seen_paths.add(path)

        lines = text.splitlines()
        total_lines = len(lines)

        start = max(0, offset)
        end = min(total_lines, start + limit)
        sliced = lines[start:end]

        numbered = []
        for i, line in enumerate(sliced):
            line_num = start + i + 1
            numbered.append(f"{line_num:>6}\t{line}")
        content = "\n".join(numbered)

        is_partial = end < total_lines
        if is_partial:
            content += (
                f"\n\n--- PARTIAL view: lines {start + 1}-{end} of {total_lines} ---"
                f"\nCall read again with offset={end} to see the next chunk."
            )

        return {
            "content": content,
            "path": str(path),
            "total_lines": total_lines,
            "offset": start,
            "limit": limit,
            "is_partial": is_partial,
            "ok": True,
        }

    meta = ToolMeta(
        name="read",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        domain="fs:read",
        estimated_latency_ms=1_000,
    )
    return tool(_read, name="read", description=_READ_DESCRIPTION, meta=meta)
