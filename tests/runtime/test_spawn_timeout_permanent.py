from __future__ import annotations

from prodagent import Agent
from prodagent.core.state.run import AgentRun
from prodagent.core.types import ErrorSeverity, ToolOutcome, ToolResult
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.runtime.coordination.accounting import SpawnAccumulator
from prodagent.runtime.coordination.parent_runtime import ParentRuntime
from prodagent.runtime.coordination.run_loop import RunLoop
from prodagent.runtime.coordination.spawn import (
    SpawnPipeline,
    short_result,
)
from prodagent.runtime.run_context import RunContext


async def test_spawn_timeout_returns_permanent_error(monkeypatch) -> None:
    child = Agent("blocker", system_prompt="plan something", description="A child that times out")

    async def _fake_run_with_timeout(self, spec, task, packet, child_run_id):
        return short_result(spec.name, "timeout", "Sub-agent timed out after 2s")

    monkeypatch.setattr(SpawnPipeline, "_run_with_timeout", _fake_run_with_timeout)

    pipeline = SpawnPipeline(
        [child],
        llm=FakeLLMAdapter(),
        hooks=None,
        framework_config=None,
        ctx=ParentRuntime(
            constraints=[],
            budget=None,
            lock_registry=None,
            parent_run_id="parent-timeout",
            checkpoint=None,
            event_log=None,
        ),
    )

    result = await pipeline.spawn("blocker", "do something")

    tr = ToolResult.from_raw(result, tool="spawn_agent")
    assert tr.outcome is ToolOutcome.ABORT, (
        f"timeout must be RED/permanent (ABORT), not YELLOW/transient — got {tr.outcome}"
    )
    assert tr.error is not None
    assert tr.error.error_severity is ErrorSeverity.RED
    assert "timed out" in tr.error.message.lower()


async def test_finalize_run_folds_spawn_accounting_without_hooks() -> None:
    """_finalize_run must fold spawn accounting even when no hooks are wired.

    Regression for the early return (``if not hooks: return``) that used to
    sit before the fold call, silently dropping sub-agent spend/turns/tokens
    whenever a run had no hooks registry.
    """
    agent = Agent("solo", system_prompt="ctx")
    ctx = RunContext(agent=agent, task="t", run_id="r1", depth=0)
    loop = RunLoop(root_agent=agent, initial_ctx=ctx, root_run_id="r1", output_schema=None)

    run = AgentRun(run_id="r1", task="t")
    accumulator = SpawnAccumulator(
        cost_usd=1.5, turns=3, input_tokens=50, output_tokens=25, spawn_count=1
    )

    await loop._finalize_run(run, ctx, None, accumulator)

    assert run.cost_usd == 1.5
    assert run.turn_count == 3
    assert run.input_tokens == 50
    assert run.output_tokens == 25
