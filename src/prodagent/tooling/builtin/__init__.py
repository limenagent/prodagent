"""Builtin tools — currently just the spill-result reader (runtime-internal)."""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name != "make_read_tool_result":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from prodagent.tooling.builtin.read_tool_result import make_read_tool_result

    globals()[name] = make_read_tool_result
    return make_read_tool_result


def __dir__() -> list[str]:
    return ["make_read_tool_result"]
