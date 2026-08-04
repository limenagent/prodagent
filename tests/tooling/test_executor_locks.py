import asyncio

import pytest

from prodagent import SideEffectLevel, ToolMeta
from prodagent.core.state import AgentRun
from prodagent.core.types import ErrorSeverity, ToolCall
from prodagent.tooling import tool
from prodagent.tooling.reliability.locks import LockRegistry


@pytest.fixture
def lock_registry():
    return LockRegistry()


@pytest.fixture
def mock_run():
    return AgentRun(run_id="test-run-123", task="Test task")


@pytest.fixture
def mock_executor():
    async def executor(call: ToolCall) -> dict:
        return {"status": "ok", "tool": call.name, "params": call.params}

    return executor


@pytest.mark.asyncio
async def test_readonly_tool_bypasses_coordination(mock_run, mock_executor, lock_registry):
    call = ToolCall(name="query_data", params={"id": 1})
    meta = ToolMeta(name="query_data", is_readonly=True, side_effect_level=SideEffectLevel.LOW)
    result = await lock_registry.execute(call, meta, mock_executor, mock_run.run_id)
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_resource_id_semaphore_serialization(mock_run, lock_registry):
    execution_count = 0

    @tool(
        name="semaphore_op",
        meta=ToolMeta(
            name="semaphore_op",
            side_effect_level=SideEffectLevel.MEDIUM,
            reversibility=0.5,
            resource_id="sem-resource",
        ),
    )
    async def semaphore_op() -> dict:
        return {}

    meta = semaphore_op.meta

    async def run_task():
        nonlocal execution_count
        call = ToolCall(name="semaphore_op", params={})

        async def executor(c: ToolCall) -> dict:
            nonlocal execution_count
            execution_count += 1
            await asyncio.sleep(0.05)
            return {"count": execution_count}

        return await lock_registry.execute(call, meta, executor, mock_run.run_id)

    tasks = [asyncio.create_task(run_task()) for _ in range(3)]
    results = await asyncio.gather(*tasks)

    assert all(r["count"] > 0 for r in results)
    assert execution_count == 3


@pytest.mark.asyncio
async def test_no_coordination_fallback(mock_run, mock_executor, lock_registry):
    call = ToolCall(name="free_tool", params={"x": 1})

    @tool(name="free_tool", meta=ToolMeta(name="free_tool", side_effect_level=SideEffectLevel.LOW))
    async def free_tool(x: int) -> dict:
        return {"x": x}

    result = await lock_registry.execute(call, free_tool.meta, mock_executor, mock_run.run_id)
    assert result["status"] == "ok"


def test_lock_registry_creates_fresh_semaphores():
    reg_a = LockRegistry()
    reg_b = LockRegistry()
    sem_a = reg_a.get_semaphore("res-x")
    sem_b = reg_b.get_semaphore("res-x")
    assert sem_a is not sem_b


def test_lock_registry_same_resource_returns_same_semaphore():
    reg = LockRegistry()
    assert reg.get_semaphore("foo") is reg.get_semaphore("foo")


def test_toolmeta_lock_fields_default_to_none():
    meta = ToolMeta(name="t")
    assert meta.lock_strategy is None


@pytest.mark.asyncio
async def test_resource_locked_returned_when_semaphore_held(mock_run, lock_registry):
    call = ToolCall(name="write_progress", params={})
    meta = ToolMeta(
        name="write_progress",
        side_effect_level=SideEffectLevel.MEDIUM,
        resource_id="progress_file",
    )
    holder_started = asyncio.Event()
    release_holder = asyncio.Event()

    async def hold_and_wait(c: ToolCall) -> dict:
        holder_started.set()
        await release_holder.wait()
        return {"ok": True}

    async def fail_if_invoked(c: ToolCall) -> dict:
        raise AssertionError("second agent must not acquire while first holds the semaphore")

    holder_task = asyncio.create_task(lock_registry.execute(call, meta, hold_and_wait, "agent-3"))
    await holder_started.wait()

    contender_result = await lock_registry.execute(
        call, meta, fail_if_invoked, "agent-4", wait_timeout=0.05
    )
    assert contender_result.error is not None
    assert contender_result.error.code == "resource_locked"
    assert contender_result.error.error_severity is ErrorSeverity.YELLOW

    release_holder.set()
    await holder_task
