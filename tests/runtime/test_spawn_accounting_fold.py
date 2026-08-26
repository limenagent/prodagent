from __future__ import annotations

import pytest

from prodagent import Agent, AgentConfig, ExecutionMode
from prodagent.coordination.spawn import ChildResult
from prodagent.kernel.budget import HardBudget
from prodagent.kernel.bus import HookRegistry
from prodagent.kernel.state import AgentRun
from prodagent.kernel.types import LLMResponse, ToolCall
from prodagent.llm import LLMConfig
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.runtime.parent_runtime import SpawnAccumulator


def fold_spawn_accounting(run: AgentRun, accumulator: SpawnAccumulator | None) -> None:
    """The runner-side entry: hop-end fold, no-op when nothing was spawned."""
    if accumulator is not None:
        accumulator.fold_into(run)


def _reactive_agent(name: str, *, context: str = "") -> Agent:
    return Agent(name, system_prompt=context, mode=ExecutionMode.REACTIVE)


def test_fold_spawn_accounting_adds_onto_existing_run_totals():
    run = AgentRun(run_id="r1", task="t")
    run.metrics.cost_usd = 0.5
    run.metrics.turn_count = 3
    run.metrics.input_tokens = 100
    run.metrics.output_tokens = 50
    run.tool_history = [ToolCall(name="existing_tool", params={})]

    accumulator = SpawnAccumulator(
        cost_usd=1.0,
        turns=2,
        input_tokens=10,
        output_tokens=5,
        spawn_count=1,
        tool_history=[ToolCall(name="child_tool", params={})],
    )

    fold_spawn_accounting(run, accumulator)

    assert run.cost_usd == pytest.approx(1.5)
    assert run.turn_count == 5
    assert run.input_tokens == 110
    assert run.output_tokens == 55
    assert [tc.name for tc in run.tool_history] == ["existing_tool", "child_tool"]


def test_fold_spawn_accounting_noop_when_accumulator_none_or_empty():
    run = AgentRun(run_id="r1", task="t")
    run.metrics.cost_usd = 0.5
    run.metrics.turn_count = 3
    run.metrics.input_tokens = 1
    run.metrics.output_tokens = 1

    fold_spawn_accounting(run, None)
    assert (run.cost_usd, run.turn_count, run.input_tokens, run.output_tokens) == (0.5, 3, 1, 1)

    fold_spawn_accounting(run, SpawnAccumulator())
    assert (run.cost_usd, run.turn_count, run.input_tokens, run.output_tokens) == (0.5, 3, 1, 1)


def test_spawn_accumulator_add_matches_child_result_fields():
    accumulator = SpawnAccumulator()
    child = ChildResult(
        agent="worker",
        state="completed",
        output="done",
        turns=2,
        cost_usd=0.3,
        input_tokens=20,
        output_tokens=10,
        tool_history=[ToolCall(name="t1", params={})],
    )

    accumulator.add(child)

    assert accumulator.cost_usd == pytest.approx(0.3)
    assert accumulator.turns == 2
    assert accumulator.input_tokens == 20
    assert accumulator.output_tokens == 10
    assert accumulator.spawn_count == 1
    assert [tc.name for tc in accumulator.tool_history] == ["t1"]


@pytest.mark.asyncio
async def test_concurrent_spawns_fold_into_parent_run_end_to_end(monkeypatch):
    monkeypatch.setattr(
        LLMConfig,
        "cost_for_response",
        lambda self, response: response.input_tokens * 0.01 + response.output_tokens * 0.02,
    )

    hooks = HookRegistry()

    child_a = _reactive_agent("childA", context="does A")
    child_a._hooks = hooks
    child_b = _reactive_agent("childB", context="does B")
    child_b._hooks = hooks

    # Spawn._run_child always builds each spawned child with
    # `llm=self._llm` — the *parent's* session LLM — never the child spec's
    # own `_llm`. So all four real LLM calls (parent's tool-call turn, each
    # child's single turn, parent's final turn) are drawn from one shared
    # queue. Concurrent spawning means it's not guaranteed which child
    # consumes which of the two child-shaped responses, so the assertions
    # below only check totals, not per-child attribution.
    parent = Agent(
        "parent",
        system_prompt="delegates to A and B",
        mode=ExecutionMode.REACTIVE,
        budget=HardBudget(max_cost_usd=100.0, max_tokens=10_000_000),
        config=AgentConfig(name="parent", agents=[child_a, child_b]),
    )
    parent.config.hooks = hooks
    parent.config.llm = FakeLLMAdapter(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(name="spawn_agent", params={"name": "childA", "task": "do A"}),
                    ToolCall(name="spawn_agent", params={"name": "childB", "task": "do B"}),
                ],
                stop_reason="tool_use",
            ),
            LLMResponse(
                content="child work 1", stop_reason="end_turn", input_tokens=100, output_tokens=10
            ),
            LLMResponse(
                content="child work 2", stop_reason="end_turn", input_tokens=200, output_tokens=20
            ),
            LLMResponse(content="parent done", stop_reason="end_turn"),
        ]
    )

    run = await parent.chat("root", session_id="spawn-fold-concurrent")

    # Parent's own two turns (tool-call + final) carry no tokens; all
    # accounting below comes from folding the two children's totals.
    assert run.turn_count == 4  # 2 parent turns + (childA:1 + childB:1) folded
    assert run.input_tokens == 300
    assert run.output_tokens == 30
    assert run.cost_usd == pytest.approx(100 * 0.01 + 10 * 0.02 + 200 * 0.01 + 20 * 0.02)


@pytest.mark.asyncio
async def test_spawned_child_run_has_parent_run_id(tmp_path, monkeypatch):
    """A spawned child's AgentRun carries parent_run_id — the explicit field
    that replaces ::-string parsing in is_child_subordinate."""
    from prodagent.core.config import FrameworkConfig
    from prodagent.kernel.state import is_child_subordinate

    monkeypatch.setattr(
        LLMConfig,
        "cost_for_response",
        lambda self, response: 0.0,
    )

    from prodagent.core.config import production

    fw = production(FrameworkConfig.default())
    fw.orchestration.runs_dir = str(tmp_path / "runs")

    child = Agent("childA", system_prompt="does A", mode=ExecutionMode.REACTIVE)
    child.config.hooks = HookRegistry()

    parent = Agent(
        "parent",
        system_prompt="delegates to A",
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(name="parent", framework=fw, agents=[child]),
    )
    parent.config.hooks = HookRegistry()
    parent.config.llm = FakeLLMAdapter(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(name="spawn_agent", params={"name": "childA", "task": "do A"})
                ],
                stop_reason="tool_use",
            ),
            LLMResponse(content="parent done", stop_reason="end_turn"),
        ]
    )
    child.config.llm = FakeLLMAdapter(
        responses=[LLMResponse(content="child work", stop_reason="end_turn")]
    )

    run = await parent.chat("root", session_id="root-1")

    # The parent run itself is not a child.
    assert run.parent_run_id is None
    assert is_child_subordinate(run) is False

    # Load the child's checkpoint — it must carry parent_run_id pointing at root.
    cp = parent._ensure_checkpoint_resolved()
    child_run = await cp.load("root-1:1::childA")
    assert child_run is not None
    assert child_run.parent_run_id == "root-1:1"
    assert is_child_subordinate(child_run) is True
