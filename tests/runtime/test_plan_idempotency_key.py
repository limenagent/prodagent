"""PLAN_FIRST nodes must inject a crash-stable idempotency key.

A dangling node (crashed between NodeStarted and NodeCompleted) is re-run on
restore. The re-run must re-derive the SAME key its first attempt used, or the
external system would treat it as a new request and execute the side effect
twice. The anchor is node_id + attempt number, both of which roll back with the
checkpoint — see chapter 14's "不丢钱" defence line 2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from prodagent import SideEffectLevel, ToolMeta
from prodagent.kernel.graph import Node, Plan
from prodagent.kernel.node_runner import NodeRunner
from prodagent.kernel.run import Run
from prodagent.kernel.units import ToolUnit
from prodagent.tooling import tool
from prodagent.tooling.dispatcher import ToolDispatcher

if TYPE_CHECKING:
    from prodagent.kernel.types import ToolCall


class _StubEventLog:
    async def record_node_started(self, plan: Plan, run: Run, node_id: str) -> int:
        return 0

    async def record_node_completed(
        self, plan: Plan, run: Run, node_id: str, result: object
    ) -> int:
        return 0


def _capturing_tool(captured: list[str]):
    @tool(
        name="refund_order",
        meta=ToolMeta(
            name="refund_order",
            side_effect_level=SideEffectLevel.MEDIUM,
            enforced_idempotent=True,
        ),
    )
    async def refund_order(order_id: str, idempotency_key: str = "") -> dict:
        captured.append(idempotency_key)
        return {"refunded": order_id}

    return refund_order


def _plan_with_node(action: str = "refund_order", params: dict | None = None) -> tuple[Plan, Node]:
    plan = Plan(plan_id="p-idem")
    node = Node(
        node_id="s1",
        body=ToolUnit(action),
        params={"order_id": "A1"} if params is None else params,
    )
    plan.add_nodes([node])
    return plan, node


def _node_runner(fn) -> NodeRunner:
    dispatcher = ToolDispatcher({fn.name: fn})
    return NodeRunner(
        _StubEventLog(),
        dispatcher=dispatcher,
        tools=(lambda call, run_id="": _execute(fn, call)),
    )


async def _execute(fn, call: ToolCall):
    return await fn(**call.params, run_id="r-idem")


@pytest.mark.asyncio
async def test_enforced_idempotent_node_gets_node_anchored_key():
    captured: list[str] = []
    runner = _node_runner(_capturing_tool(captured))
    plan, node = _plan_with_node()
    run = Run(run_id="r-idem", task="t")

    await runner.run_one(node, plan, run)

    assert captured == ["r-idem:s1:a1"]


@pytest.mark.asyncio
async def test_reexecuted_dangling_node_rederives_same_key():
    """Crash between side-effect and NodeCompleted: the restored re-run must
    hit the external system with the same key (duplicate suppressed)."""
    first_attempt: list[str] = []
    runner = _node_runner(_capturing_tool(first_attempt))
    plan, node = _plan_with_node()
    run = Run(run_id="r-idem", task="t")
    await runner.run_one(node, plan, run)
    assert first_attempt == ["r-idem:s1:a1"]

    # What the checkpoint holds: the node had not finished when it was saved —
    # the NODE_STARTED event replays to "running", which from_state flips back
    # to PENDING with attempts rolled back (rerun, not skip).
    checkpoint_state = {
        "version": 1,
        "nodes": {
            "s1": {
                "node_id": "s1",
                "action": "refund_order",
                "params": {"order_id": "A1"},
                "depends_on": [],
                "status": "running",
                "attempts": 0,
            }
        },
    }
    restored_plan, restored_states = Plan.from_state(checkpoint_state, plan_id="p-idem")
    restored_node = restored_plan.get_node("s1")
    assert restored_node is not None
    assert restored_states["s1"].status.value == "pending"
    assert restored_states["s1"].attempts == 0

    rerun_keys: list[str] = []
    rerun_runner = _node_runner(_capturing_tool(rerun_keys))
    rerun_run = Run(run_id="r-idem", task="t")
    rerun_run.node_states = restored_states
    await rerun_runner.run_one(restored_node, restored_plan, rerun_run)

    assert rerun_keys == first_attempt


@pytest.mark.asyncio
async def test_model_supplied_key_not_overwritten():
    captured: list[str] = []
    fn = _capturing_tool(captured)
    runner = _node_runner(fn)
    plan, node = _plan_with_node(params={"order_id": "A1", "idempotency_key": "client-supplied"})
    run = Run(run_id="r-idem", task="t")

    await runner.run_one(node, plan, run)

    assert captured == ["client-supplied"]


@pytest.mark.asyncio
async def test_non_enforced_node_gets_no_key():
    captured: list[str] = []

    @tool(name="plain_read", readonly=True)
    async def plain_read(idempotency_key: str = "") -> dict:
        captured.append(idempotency_key)
        return {"ok": True}

    runner = _node_runner(plain_read)
    plan, node = _plan_with_node(action="plain_read", params={})
    run = Run(run_id="r-idem", task="t")

    await runner.run_one(node, plan, run)

    assert captured == [""]
