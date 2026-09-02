"""The five node bodies and the runner that interprets them.

Law under test: a body declares *what*, the runner injects *how* — fn
results, model text, and governed ToolResults all come back through one
spout, errors float (the node runner classifies), and the durable wire
round-trips every kind losslessly.
"""

from __future__ import annotations

import pytest

from prodagent.kernel.bodies.base import (
    FnBody,
    LLMBody,
    NodeKind,
    ReActBody,
    SubAgentBody,
    ToolBody,
    body_from_wire,
    body_to_wire_extras,
)
from prodagent.kernel.bodies.runner import BodyRunner
from prodagent.kernel.state import AgentRun
from prodagent.kernel.types import ToolCall, ToolOutcome, ToolResult


def _run() -> AgentRun:
    return AgentRun(run_id="r-bodies", task="t")


def _call(params: dict | None = None) -> ToolCall:
    return ToolCall(name="x", params=params or {}, call_id="c1")


class TestDispatch:
    async def test_tool_body_goes_through_the_injected_executor(self):
        seen: list[ToolCall] = []

        async def tools(call: ToolCall, *, run_id: str = "") -> ToolResult:
            seen.append(call)
            return ToolResult(ToolOutcome.OK, value={"ok": True}, tool=call.name)

        runner = BodyRunner(tools)
        run = _run()
        result = await runner.run(ToolBody("search"), _call({"q": "x"}), run)

        assert isinstance(result, ToolResult)
        assert result.outcome is ToolOutcome.OK
        assert seen[0].params == {"q": "x"}

    async def test_fn_body_invokes_sync_and_async_functions(self):
        async def double(x: int) -> int:
            return x * 2

        def shout(text: str) -> str:
            return text.upper()

        runner = BodyRunner(tools=None, fns={"double": double, "shout": shout})  # type: ignore[arg-type]
        run = _run()
        assert await runner.run(FnBody("double"), _call({"x": 21}), run) == 42
        assert await runner.run(FnBody("shout"), _call({"text": "hi"}), run) == "HI"

    async def test_fn_body_with_unknown_name_names_the_offender(self):
        runner = BodyRunner(tools=None, fns={})  # type: ignore[arg-type]
        with pytest.raises(KeyError, match="no function registered"):
            await runner.run(FnBody("ghost"), _call(), _run())

    async def test_llm_body_calls_the_invoker_with_prompt_and_system(self):
        calls: list[tuple[str, str]] = []

        async def llm(prompt: str, *, system: str = "", run_id: str = "") -> str:
            calls.append((prompt, system))
            return f"echo:{prompt}"

        runner = BodyRunner(tools=None, llm=llm)  # type: ignore[arg-type]
        result = await runner.run(
            LLMBody(prompt="summarize this", system="be terse"), _call(), _run()
        )

        assert result == "echo:summarize this"
        assert calls == [("summarize this", "be terse")]

    async def test_llm_body_param_overrides_the_declared_prompt(self):
        """Upstream output flows into a fixed-prompt step via {{dep.output}}
        bound to "prompt" — resolved params outrank the declaration."""

        async def llm(prompt: str, *, system: str = "", run_id: str = "") -> str:
            return prompt

        runner = BodyRunner(tools=None, llm=llm)  # type: ignore[arg-type]
        result = await runner.run(
            LLMBody(prompt="declared"), _call({"prompt": "from upstream"}), _run()
        )
        assert result == "from upstream"

    async def test_llm_body_without_invoker_fails_loudly(self):
        runner = BodyRunner(tools=None)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="no LLM invoker wired"):
            await runner.run(LLMBody(prompt="hi"), _call(), _run())

    async def test_unwired_engines_fail_loudly(self):
        runner = BodyRunner(tools=None)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="no ReactEngine wired"):
            await runner.run(ReActBody(), _call(), _run())
        with pytest.raises(RuntimeError, match="no activation wired"):
            await runner.run(SubAgentBody(agent="researcher"), _call(), _run())


class TestBodyMetadata:
    def test_readonly_flags_match_the_side_effect_classes(self):
        assert FnBody("x").readonly is True  # pure function
        assert LLMBody("p").readonly is True  # processes input, decides nothing
        assert ToolBody("x").readonly is None  # ask the registry
        assert ReActBody().readonly is False  # acts autonomously
        assert SubAgentBody("a").readonly is False

    def test_targets_are_the_display_names(self):
        assert FnBody("normalize").target == "normalize"
        assert ToolBody("search").target == "search"
        assert LLMBody("p").target == "llm"
        assert ReActBody().target == "react"
        assert SubAgentBody("researcher").target == "researcher"

    def test_kinds_tag_the_ladder(self):
        assert FnBody("x").kind is NodeKind.FN
        assert ToolBody("x").kind is NodeKind.TOOL
        assert LLMBody("p").kind is NodeKind.LLM
        assert ReActBody().kind is NodeKind.REACT
        assert SubAgentBody("a").kind is NodeKind.SUBAGENT


class TestBodyWire:
    def test_every_kind_round_trips(self):
        for body in [
            FnBody("normalize"),
            ToolBody("search"),
            LLMBody(prompt="summarize", system="terse"),
            ReActBody(),
            SubAgentBody(agent="researcher", task="dig in"),
        ]:
            extras = body_to_wire_extras(body)
            rebuilt = body_from_wire(body.kind.value, body.target, extras)
            assert rebuilt == body

    def test_kindless_legacy_wire_reads_as_a_tool_body(self):
        assert body_from_wire("", "search", {}) == ToolBody("search")
