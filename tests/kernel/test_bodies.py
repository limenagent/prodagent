"""The five built-in bodies — declaration plus context, no interpreter.

Law under test: a unit declares *what* (names and prompts, frozen,
serializable), the NodeContext injects *how* (the tool throat, the fn
table, the model invoker) — fn results, model text, and governed
ToolResults all come back through one Outcome.value, errors float (the
driver classifies), live events ride the tap, and the durable wire
round-trips every kind losslessly.
"""

from __future__ import annotations

from typing import Any

import pytest

from prodagent.kernel.bodies import (
    FnBody,
    LLMBody,
    NodeKind,
    SubPlanBody,
    ToolBody,
    body_from_wire,
    body_to_wire_extras,
)
from prodagent.kernel.body import NodeContext
from prodagent.kernel.run import Run
from prodagent.kernel.types import ToolCall, ToolOutcome, ToolResult


def _loop(*, goal: str = ""):
    from prodagent.runtime.recipes.loop_body import LoopBody

    return LoopBody(goal=goal)


def _run() -> Run:
    return Run(run_id="r-units", task="t")


def _call(params: dict | None = None) -> ToolCall:
    return ToolCall(name="x", params=params or {}, call_id="c1")


def _ctx(**slots: Any) -> NodeContext:
    # the loop driver rides the wiring bag under its convention key
    from prodagent.runtime.recipes.loop_body import LOOP_DRIVER_KEY

    if "wiring_loop" in slots:
        slots["wiring"] = {LOOP_DRIVER_KEY: slots.pop("wiring_loop")}
    return NodeContext(run_id="r-units", **slots)


class TestDispatch:
    async def test_tool_unit_goes_through_the_contexts_executor(self):
        seen: list[ToolCall] = []

        async def tools(call: ToolCall, *, run_id: str = "") -> ToolResult:
            seen.append(call)
            return ToolResult(ToolOutcome.OK, value={"ok": True}, tool=call.name)

        outcome = await ToolBody("search").run(_call({"q": "x"}), _ctx(tools=tools))

        assert isinstance(outcome.value, ToolResult)
        assert outcome.value.outcome is ToolOutcome.OK
        assert seen[0].params == {"q": "x"}

    async def test_fn_unit_invokes_sync_and_async_functions(self):
        async def double(x: int) -> int:
            return x * 2

        def shout(text: str) -> str:
            return text.upper()

        ctx = _ctx(fns={"double": double, "shout": shout})
        assert (await FnBody("double").run(_call({"x": 21}), ctx)).value == 42
        assert (await FnBody("shout").run(_call({"text": "hi"}), ctx)).value == "HI"

    async def test_fn_unit_with_unknown_name_names_the_offender(self):
        with pytest.raises(KeyError, match="no function registered"):
            await FnBody("ghost").run(_call(), _ctx(fns={}))

    async def test_llm_unit_calls_the_invoker_with_prompt_and_system(self):
        calls: list[tuple[str, str]] = []

        async def llm(prompt: str, *, system: str = "", run_id: str = "") -> str:
            calls.append((prompt, system))
            return f"echo:{prompt}"

        outcome = await LLMBody(prompt="summarize this", system="be terse").run(
            _call(), _ctx(llm=llm)
        )

        assert outcome.value == "echo:summarize this"
        assert calls == [("summarize this", "be terse")]

    async def test_llm_unit_param_overrides_the_declared_prompt(self):
        """Upstream output flows into a fixed-prompt step via {{dep.output}}
        bound to "prompt" — resolved params outrank the declaration."""

        async def llm(prompt: str, *, system: str = "", run_id: str = "") -> str:
            return prompt

        outcome = await LLMBody(prompt="declared").run(
            _call({"prompt": "from upstream"}), _ctx(llm=llm)
        )
        assert outcome.value == "from upstream"

    async def test_unwired_slots_fail_loudly(self):
        with pytest.raises(RuntimeError, match="no tool executor"):
            await ToolBody("search").run(_call(), _ctx())
        with pytest.raises(RuntimeError, match="no model invoker"):
            await LLMBody(prompt="hi").run(_call(), _ctx())
        with pytest.raises(RuntimeError, match="no loop driver"):
            await _loop().run(_call(), _ctx())
        with pytest.raises(RuntimeError, match="no activation"):
            await SubPlanBody(agent="researcher").run(_call(), _ctx())


class _FakeEngine:
    """Duck-typed AgentLoop: two fired events, one folded outcome."""

    def __init__(self) -> None:
        self.drove: list[str] = []

    async def drive(self, run: Run, *, goal: str = "", settle_run: bool = True):
        for marker in ("turn-1", "turn-2"):
            yield marker
        self.drove.append(goal or "<task>")

    def outcome_of(self, run: Run, *, goal_scope: bool = False) -> ToolResult:
        return ToolResult(ToolOutcome.OK, value=f"done(goal={goal_scope})", tool="react")


class TestAutonomousUnit:
    async def test_streaming_form_yields_turns_inline_and_boxes_the_outcome(self):
        seen: list[Any] = []
        box: list = []
        ctx = _ctx(run=_run(), wiring_loop=_FakeEngine())

        async for event in _loop().run_stream(_call(), ctx, box):
            seen.append(event)

        assert seen == ["turn-1", "turn-2"]  # yielded as they happen
        assert box[0].value.value == "done(goal=False)"

    async def test_draining_form_is_the_same_execution(self):
        ctx = _ctx(run=_run(), wiring_loop=_FakeEngine())

        outcome = await _loop().run(_call(), ctx)

        assert outcome.value.value == "done(goal=False)"

    async def test_goal_scopes_the_loop_and_settles_the_node_not_the_run(self):
        engine = _FakeEngine()
        ctx = _ctx(run=_run(), wiring_loop=engine)

        outcome = await _loop(goal="find the bug").run(_call(), ctx)

        assert engine.drove == ["find the bug"]  # goal mode drives scoped
        assert outcome.value.value == "done(goal=True)"

    async def test_without_a_live_run_it_fails_loudly(self):
        with pytest.raises(RuntimeError, match="no live run"):
            await _loop().run(_call(), _ctx(wiring_loop=_FakeEngine()))


class TestSubAgentUnit:
    async def test_completed_child_carries_the_report(self):
        async def subagent(agent: str, task: str, run_id: str = "") -> dict[str, Any]:
            return {"agent": agent, "state": "completed", "output": "found 3 issues"}

        outcome = await SubPlanBody(agent="scout", task="dig").run(_call(), _ctx(subagent=subagent))
        assert outcome.value["output"] == "found 3 issues"

    async def test_suspended_child_parks_on_the_childs_approval(self):
        async def subagent(agent: str, task: str, run_id: str = "") -> dict[str, Any]:
            return {"state": "suspended", "approval_request_id": "ap-1"}

        outcome = await SubPlanBody(agent="scout").run(_call(), _ctx(subagent=subagent))
        assert outcome.value.outcome is ToolOutcome.SUSPENDED
        assert outcome.value.approval_request_id == "ap-1"

    async def test_failed_child_becomes_red_feedback(self):
        async def subagent(agent: str, task: str, run_id: str = "") -> dict[str, Any]:
            return {"state": "failed", "output": "child exploded"}

        outcome = await SubPlanBody(agent="scout").run(_call(), _ctx(subagent=subagent))
        assert outcome.value.outcome is ToolOutcome.ABORT
        assert "child exploded" in outcome.value.error.message

    async def test_upstream_task_params_outrank_the_declaration(self):
        seen: list[str] = []

        async def subagent(agent: str, task: str, run_id: str = "") -> dict[str, Any]:
            seen.append(task)
            return {"state": "completed", "output": ""}

        await SubPlanBody(agent="scout", task="declared").run(
            _call({"task": "from upstream"}), _ctx(subagent=subagent)
        )
        assert seen == ["from upstream"]


class TestUnitMetadata:
    def test_readonly_flags_match_the_side_effect_classes(self):
        assert FnBody("x").readonly is True  # pure function
        assert LLMBody("p").readonly is True  # processes input, decides nothing
        assert ToolBody("x").readonly is None  # ask the registry
        assert _loop().readonly is False  # acts autonomously
        assert SubPlanBody("a").readonly is False

    def test_targets_are_the_display_names(self):
        assert FnBody("normalize").target == "normalize"
        assert ToolBody("search").target == "search"
        assert LLMBody("p").target == "llm"
        assert _loop().target == "loop"
        assert SubPlanBody("researcher").target == "researcher"

    def test_kinds_tag_the_ladder(self):
        assert FnBody("x").kind is NodeKind.FN
        assert ToolBody("x").kind is NodeKind.TOOL
        assert LLMBody("p").kind is NodeKind.LLM
        from prodagent.runtime.recipes.loop_body import LOOP_DRIVER_KEY

        assert _loop().kind == "loop"  # a recipe body: plain string kind
        assert LOOP_DRIVER_KEY == "loop_driver"
        assert SubPlanBody("a").kind is NodeKind.SUBAGENT


class TestUnitWire:
    def test_every_builtin_kind_round_trips(self):
        for unit in [
            FnBody("normalize"),
            ToolBody("search"),
            LLMBody(prompt="summarize", system="terse"),
            SubPlanBody(agent="researcher", task="dig in"),
        ]:
            extras = body_to_wire_extras(unit)
            rebuilt = body_from_wire(unit.kind.value, unit.target, extras)
            assert rebuilt == unit

    def test_a_loop_body_is_process_local_and_refuses_the_wire(self):
        """The loop is a recipe body, not a kernel built-in: it never
        round-trips (composition is re-declared in code, ruling 3), and the
        pre-split 'autonomous' wire kind fails with a pointed message."""
        import pytest as _pytest

        with _pytest.raises(ValueError, match="predates the loop-body split"):
            body_from_wire("autonomous", "loop", {})
        with _pytest.raises(ValueError, match="not a kernel built-in"):
            body_from_wire("loop", "loop", {})

    def test_kindless_legacy_wire_reads_as_a_tool_unit(self):
        assert body_from_wire("", "search", {}) == ToolBody("search")
