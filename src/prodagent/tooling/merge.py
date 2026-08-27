"""Tool-list merging — append without duplicating names."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from prodagent.ports import Tool

__all__ = ["merge_tools_by_name", "attach_tools"]


def merge_tools_by_name(existing: list[Tool], new: Iterable[Tool]) -> list[Tool]:
    """Append tools from ``new`` whose name isn't already in ``existing``. Returns what was added."""
    names = {t.name for t in existing}
    added: list[Tool] = []
    for tool in new:
        if tool.name not in names:
            existing.append(tool)
            names.add(tool.name)
            added.append(tool)
    return added


def attach_tools(
    active_tools: list[Tool],
    tool_schemas: list[dict[str, Any]],
    new_tools: Iterable[Tool],
) -> list[Tool]:
    """Merge ``new_tools`` into the hop's active tool list and schema list."""
    added = merge_tools_by_name(active_tools, new_tools)
    tool_schemas.extend(t.schema for t in added)
    return added
