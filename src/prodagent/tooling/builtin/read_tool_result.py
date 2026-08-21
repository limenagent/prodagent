from __future__ import annotations

import logging
import math
import re
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from prodagent.core.error_reason import ErrorReason
from prodagent.core.types import SideEffectLevel, ToolError, ToolMeta
from prodagent.tooling.decorator import tool

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.tooling.base import FunctionTool

logger = logging.getLogger(__name__)

__all__ = ["make_read_tool_result"]


_READ_TOOL_RESULT_DESCRIPTION = (
    "Read a tool-result file that was spilled to disk because it was too large "
    "to keep in context. Always prefer `grep_pattern` to find specific entries "
    "(a pool name, an error class, a CHG number) rather than reading the whole "
    "file - spilled files can be thousands of lines. Use offset/limit to page "
    "through results. This is the only way to see the full content of a spilled "
    "result; the in-context <spilled> placeholder shows only a short preview."
)


def _make_matcher(pattern: str) -> Callable[[str], bool]:
    try:
        rx = re.compile(pattern, re.IGNORECASE)
        return lambda ln: rx.search(ln) is not None
    except re.error:
        sub = pattern.lower()
        return lambda ln: sub in ln.lower()


def _query_lines(
    text: str, *, grep_pattern: str | None, offset: int, limit: int, name: str
) -> tuple[str, int, int]:
    lines = text.splitlines()
    if grep_pattern:
        match = _make_matcher(grep_pattern)
        matches = [f"{i + 1}: {ln}" for i, ln in enumerate(lines) if match(ln)]
        if not matches:
            return f"(no lines matching {grep_pattern!r} in {name})", 0, 0
        if offset >= len(matches):
            return (
                (
                    f"offset {offset} >= {len(matches)} matches in {name}; "
                    f"no more results. Stop paging - lower the offset or refine the pattern."
                ),
                len(matches),
                0,
            )
        sliced = matches[offset : offset + limit]
        header = f"{len(matches)} match(es) for {grep_pattern!r} in {name}; showing {len(sliced)} from match {offset + 1}:\n"
        return header + "\n".join(sliced), len(matches), len(sliced)

    if offset >= len(lines):
        return (
            (
                f"offset {offset} >= {len(lines)} lines in {name}; "
                f"no more results. Stop paging - lower the offset."
            ),
            len(lines),
            0,
        )
    sliced = lines[offset : offset + limit]
    header = f"{len(lines)} line(s) in {name}; showing {len(sliced)} from line {offset + 1}:\n"
    return header + "\n".join(sliced), len(lines), len(sliced)


def make_read_tool_result(spill_store: ToolResultSpillStore) -> FunctionTool:
    # One tool per agent — spill store is per-agent, so the tool closes over it.

    async def _read_tool_result(
        path: Annotated[
            str,
            Field(
                description=(
                    "The `path` value from a <spilled> placeholder. Pass it verbatim - "
                    "the tool confines reads to the spill directory."
                )
            ),
        ],
        grep_pattern: Annotated[
            str | None,
            Field(
                description=(
                    "Case-insensitive regex to filter lines by. Use this to find "
                    "specific pool names, error classes, or alert signatures (e.g. "
                    "'labelservice|labelbilling', 'CHG\\d+'). Returns matching lines "
                    "with line numbers. Omit to read a raw slice."
                )
            ),
        ] = None,
        offset: Annotated[
            int,
            Field(
                description="Starting line index (0-based). For grep results, offsets into the match list."
            ),
        ] = 0,
        limit: Annotated[
            int, Field(description="Maximum number of lines (or grep matches) to return.")
        ] = 100,
    ) -> dict[str, Any] | ToolError:
        try:
            resolved = spill_store.resolve(path)
            text = spill_store.read_raw(path)
            if text is None:
                return ToolError.from_reason(
                    ErrorReason.FORMAT_ERROR,
                    code="spill_file_not_found",
                    message=f"Spill file {path!r} not found.",
                    hint="Check the path from the <spilled> placeholder.",
                )
            result, total, shown = _query_lines(
                text,
                grep_pattern=grep_pattern,
                offset=offset,
                limit=limit,
                name=resolved.name,
            )
            return {
                "content": result,
                "path": path,
                "ok": True,
                "lines_total": total,
                "lines_shown": shown,
            }
        except ValueError as exc:
            logger.warning("read_tool_result rejected path %r: %s", path, exc)
            return ToolError.from_reason(
                ErrorReason.FORMAT_ERROR,
                code="invalid_spill_path",
                message=str(exc),
                hint="Use the path value verbatim from the <spilled> placeholder.",
            )

    meta = ToolMeta(
        name="read_tool_result",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        domain="prodagent:spill",
        timeout_seconds=10.0,
        max_result_chars=math.inf,
    )
    return tool(
        _read_tool_result,
        name="read_tool_result",
        description=_READ_TOOL_RESULT_DESCRIPTION,
        meta=meta,
    )
