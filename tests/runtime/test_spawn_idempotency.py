from __future__ import annotations

from prodagent.coordination.spawn import build_spawn_tools_for_agent
from prodagent.kernel.state import AgentRun
from prodagent.kernel.types import ToolCall, ToolResultEvent
from prodagent.llm.fake import script
from prodagent.runtime.agent import Agent
from prodagent.tooling.dispatcher import ToolDispatcher


async def _run_batch(runner: ToolDispatcher, run: AgentRun, calls: list[ToolCall]) -> list[dict]:
    results = []
    async for event in runner.run_batch(run, calls):
        if isinstance(event, ToolResultEvent):
            results.append(event.result.to_wire())
    return results


async def test_runner_injects_stable_key_and_retry_dedupes_via_handoff():
    fake_llm = script({"content": "done"})
    worker = build_spawn_tools_for_agent([Agent("worker", system_prompt="work")], llm=fake_llm)
    assert worker is not None
    runner = ToolDispatcher({"spawn_agent": worker.tool})

    run = AgentRun(run_id="R1", task="t")
    call = ToolCall(name="spawn_agent", params={"name": "worker", "task": "do it"})

    [first] = await _run_batch(runner, run, [call])
    assert first["state"] != "duplicate"
    assert worker.accumulator.spawn_count == 1

    [second] = await _run_batch(runner, run, [call])
    assert second["state"] == "duplicate"
    assert worker.accumulator.spawn_count == 1, "retry must not double-spawn the child"


async def test_different_batch_index_gets_a_different_key_and_is_not_deduped():
    fake_llm = script({"content": "done"}, {"content": "done"})
    worker = build_spawn_tools_for_agent([Agent("worker", system_prompt="work")], llm=fake_llm)
    assert worker is not None
    runner = ToolDispatcher({"spawn_agent": worker.tool})

    run = AgentRun(run_id="R2", task="t")
    calls = [
        ToolCall(name="spawn_agent", params={"name": "worker", "task": "do it"}),
        ToolCall(name="spawn_agent", params={"name": "worker", "task": "do it"}),
    ]

    results = await _run_batch(runner, run, calls)
    assert all(r["state"] != "duplicate" for r in results), results
    assert worker.accumulator.spawn_count == 2
