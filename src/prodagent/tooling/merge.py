"""Tool-list merging — append without duplicating names."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from prodagent.ports import Tool

__all__ = ["merge_tools_by_name", "attach_tools"]


def merge_tools_by_name(existing: list[Tool], new: Iterable[Tool]) -> list[Tool]:
    """Append ``new`` tools whose name isn't already taken; returns what was added.

    First-listed wins, deliberately: the merge order in the factory (inline
    → registry → MCP → spill → stage) encodes "closest to the developer has
    priority", so a remote MCP tool can never silently replace one the user
    wrote. Priority lives in one explicit rule, not in implicit overrides."""

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
    """Merge ``new_tools`` into the hop's active tool list and schema list.

    Keeps the two lists consistent in one move — a tool that joins the
    callable set must join the schema set in the same breath, or the model
    sees a tool it can't call / a call it can't see."""
    added = merge_tools_by_name(active_tools, new_tools)
    tool_schemas.extend(t.schema for t in added)
    return added
