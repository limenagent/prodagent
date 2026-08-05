"""Shared type aliases — reduce ``dict[str, Any]`` proliferation."""

from __future__ import annotations

from typing import Any, TypeAlias

#: Arbitrary JSON-serializable dict — use sparingly.
JsonDict: TypeAlias = dict[str, Any]

#: Tool parameter bag passed from LLM to tool function.
ToolParams: TypeAlias = dict[str, Any]

#: JSON Schema dict for a single tool's input schema.
ToolSchema: TypeAlias = dict[str, Any]
