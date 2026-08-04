from __future__ import annotations

from prodagent import tool


def test_decorator_accepts_plain_string_description():
    @tool(name="ping", description="a simple ping")
    async def ping() -> str:
        return "pong"

    assert ping.schema["description"] == "a simple ping"


def test_decorator_falls_back_to_docstring_when_no_description():
    @tool(name="docd")
    async def docd() -> str:
        """From the docstring."""
        return "x"

    assert docd.schema["description"] == "From the docstring."


def test_description_does_not_touch_tool_meta():

    @tool(description="x")
    async def f() -> str:
        return "x"

    assert f.meta.side_effect_level.value == "low"
    assert f.meta.reversibility is None
