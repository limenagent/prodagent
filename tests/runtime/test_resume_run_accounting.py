"""Resume accounting — a resumed run continues the SAME logical execution.

Regression guard for the PLAN_FIRST resume path: the run rebuilt by
``PlanBootstrap.prepare`` used to start empty, so ``tool_history`` /
``turn_count`` lost everything before the suspend. Eval trajectories broke
(pre-approval steps vanished) and REACTIVE idempotency seqs would re-derive
already-consumed keys. ``restore_plan`` now hydrates the accounting fields
from the checkpoint.
"""

from __future__ import annotations

from prodagent import Agent, AgentConfig, ExecutionMode, HardBudget, script, tool
from prodagent.core.types import RunState, SideEffectLevel, ToolMeta


@tool(name="low_step", readonly=True)
async def low_step(x: str) -> str:
    return f"low:{x}"


@tool(name="high_step", meta=ToolMeta(name="high_step", side_effect_level=SideEffectLevel.HIGH))
async def high_step(x: str) -> str:
    return f"high:{x}"


_PLAN = (
    '{"steps": ['
    '{"id":"s1","action":"low_step","params":{"x":"first"},"depends_on":[]},'
    '{"id":"s2","action":"high_step","params":{"x":"second"},"depends_on":["s1"]}'
    "]}"
)


def _agent(session: str) -> Agent:
    return Agent(
        "resume-accounting",
        tools=[low_step, high_step],
        mode=ExecutionMode.PLAN_FIRST,
        budget=HardBudget(max_turns=6),
        config=AgentConfig(
            name="resume-accounting",
            llm=script({"content": _PLAN}, {"content": "done"}),
        ),
    )


async def test_resumed_run_carries_full_history_and_turns(tmp_path):
    agent = _agent("resume-accounting-1")
    run = await agent.chat("do it", session_id="resume-accounting-1")

    assert run.state is RunState.SUSPENDED  # HIGH tool hit the approval gate
    assert [t.name for t in run.tool_history] == ["low_step"]
    turns_before = run.turn_count
    assert turns_before >= 1

    await agent.submit_approval(run.pending_approval_id, "approve")
    resumed = await agent.chat(resume=True, session_id="resume-accounting-1")

    assert resumed.state is RunState.COMPLETED
    # Full trajectory, not just the post-resume segment.
    assert [t.name for t in resumed.tool_history] == ["low_step", "high_step"]
    # Turns accumulate across the suspend; they never restart from zero.
    assert resumed.turn_count >= turns_before
