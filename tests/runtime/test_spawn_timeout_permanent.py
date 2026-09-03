from __future__ import annotations

from prodagent import Agent, AgentConfig
from prodagent.coordination.spawn import (
    Spawn,
    short_result,
)
from prodagent.kernel.budget import SpawnAccumulator
from prodagent.kernel.run import Run
from prodagent.kernel.types import ErrorSeverity, ToolOutcome
from prodagent.kernel.unit import coerce_result
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.runtime.parent_runtime import ParentRuntime
from prodagent.runtime.runner import InProcessRunner, RunContext, RunLoop


async def test_spawn_timeout_returns_permanent_error(monkeypatch) -> None:
    child = Agent(
        "blocker",
        system_prompt="plan something",
        config=AgentConfig(name="blocker", description="A child that times out"),
    )

    async def _fake_run_with_timeout(self, spec, task, packet, child_run_id):
        return short_result(spec.name, "timeout", "Sub-agent timed out after 2s")

    monkeypatch.setattr(Spawn, "_run_with_timeout", _fake_run_with_timeout)

    pipeline = Spawn(
        [child],
        runner=InProcessRunner(
            ParentRuntime(
                constraints=[],
                budget=None,
                parent_run_id="parent-timeout",
                checkpoint=None,
                event_log=None,
                llm=FakeLLMAdapter(),
            )
        ),
        hooks=None,
        framework_config=None,
        parent_run_id="parent-timeout",
    )

    result = await pipeline.spawn("blocker", "do something")

    tr = coerce_result(result, tool="spawn_agent")
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

    run = Run(run_id="r1", task="t")
    accumulator = SpawnAccumulator(
        cost_usd=1.5, turns=3, input_tokens=50, output_tokens=25, spawn_count=1
    )

    await loop._finalize_run(run, ctx, None, accumulator)

    assert run.cost_usd == 1.5
    assert run.turn_count == 3
    assert run.input_tokens == 50
    assert run.output_tokens == 25
