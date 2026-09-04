"""验收（2026-09-04 事故回放）: peer 上的审批挂起必须在 peer 上下文恢复。

事故形态: investigator handoff → remediator peer 的 rollback（HIGH）挂起在
审批门 → 人工批准 → 恢复。修复前的行为: bootstrap 重建"只含根的一节点图"，
parked call 以 investigator 的身份重试 → 永久 RED → rollback 从未执行，
remediator 的剩余计划全部成孤儿，run 假完成（COMPLETED）。

修复后的定律:
- 恢复走唯一咽喉；peer 节点按名重声明（身份在 wire，body 来自名册）；
- parked call 在 PEER 的工具表里 verbatim 重试恰一次（批准后才落地）；
- 根节点 COMPLETED 永不重跑——根的 LLM 轮次在恢复前后不增；
- interrupt 以节点身份结构化（node_id 指向 peer 节点），不是六个住处的合奏。
"""

from __future__ import annotations

import pytest

from prodagent import (
    Agent,
    AgentConfig,
    HardBudget,
    RoutingFakeLLM,
    RunState,
    SideEffectLevel,
    ToolMeta,
)
from prodagent.backends.factory import in_memory_checkpoint_store
from prodagent.hooks.approval import ApprovalDecision
from prodagent.hooks.bundles.security import ApprovalHooks
from prodagent.kernel.interrupt import InterruptKind
from prodagent.kernel.types import LLMResponse, ToolCall
from prodagent.runtime.runner import drive_stream, find_approval_gate
from prodagent.tooling import tool

ROLLBACK_CALLS: list[str] = []


@tool(name="rollback", meta=ToolMeta(name="rollback", side_effect_level=SideEffectLevel.HIGH))
async def rollback(sha: str) -> dict:
    ROLLBACK_CALLS.append(sha)
    return {"rolled_back_to": sha}


class _RootRounds:
    """investigate 的轮次计数 —— 恢复前后不增即"根永不重跑"的读数。"""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, messages) -> LLMResponse:
        self.count += 1
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    name="handoff_to_remediator",
                    params={"task": "payment-service 回滚到 f8c01d4"},
                    call_id="h1",
                )
            ],
            stop_reason="tool_use",
        )


def _llm() -> tuple[RoutingFakeLLM, _RootRounds]:
    llm = RoutingFakeLLM()
    root = _RootRounds()
    llm.add("investigate", [root])
    llm.add(
        "remediator",
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(name="rollback", params={"sha": "f8c01d4"}, call_id="r1")],
                stop_reason="tool_use",
            ),
            LLMResponse(
                content="rolled back to f8c01d4 and verified",
                stop_reason="end_turn",
            ),
        ],
    )
    return llm, root


def _agent(llm: RoutingFakeLLM) -> Agent:
    # 两个 drive（挂起段与恢复段）共享同一 checkpoint store —— 显式持久化
    # 是 AgentConfig 的 opt-in；恢复咽喉从这里读回 park 快照。
    approval = ApprovalHooks()
    checkpoint = in_memory_checkpoint_store()
    remediator = Agent(
        "remediator",
        tools=[rollback],
        system_prompt="You remediate incidents safely.",
        budget=HardBudget(max_turns=8),
        config=AgentConfig(
            name="remediator", llm=llm, extensions=[approval], checkpoint=checkpoint
        ),
    )
    return Agent(
        "investigate",
        tools=[],
        system_prompt="You investigate production alerts.",
        budget=HardBudget(max_turns=8),
        config=AgentConfig(
            name="investigate",
            llm=llm,
            extensions=[approval],
            checkpoint=checkpoint,
            peers=[remediator],
        ),
    )


async def _collect(agent: Agent, run_id: str):
    events = []
    async for event in drive_stream(agent, "支付服务有告警。", run_id=run_id):
        events.append(event)
    return events


@pytest.mark.asyncio
async def test_approval_parks_at_the_peer_and_resumes_in_peer_context():
    ROLLBACK_CALLS.clear()
    llm, root_rounds = _llm()
    agent = _agent(llm)

    # ── 第一段: handoff → peer 的 rollback 挂在审批门 ──
    events1 = await _collect(agent, "acc-1")
    run1 = events1[-1].run
    assert run1.state is RunState.SUSPENDED
    iv = run1.interrupt
    assert iv is not None and iv.kind is InterruptKind.APPROVE
    # 挂起事实自带节点身份: 是 peer 节点在等，不是根
    assert iv.node_id.startswith("peer:remediator#")
    assert iv.staged_call() is not None and iv.staged_call().name == "rollback"
    assert ROLLBACK_CALLS == [], "批准前 HIGH 工具绝不落地"
    assert root_rounds.count == 1

    # ── 批准 ──
    gate = find_approval_gate(agent)
    assert gate is not None
    await gate.submit_decision(iv.request_id, ApprovalDecision.APPROVE)

    # ── 第二段: 恢复 —— parked call 在 peer 上下文 verbatim 重试 ──
    events2 = await _collect(agent, "acc-1")
    run2 = events2[-1].run
    assert run2.state is RunState.COMPLETED
    assert ROLLBACK_CALLS == ["f8c01d4"], "rollback 恰好执行一次，且在批准之后"
    # 根的轮次不增: COMPLETED 的根节点永不重跑
    assert root_rounds.count == 1
    # peer 走完了它的计划: 恢复后模型才被再次询问，产出最终答案
    assert run2.final_output is not None and "rolled back" in run2.final_output


@pytest.mark.asyncio
async def test_reject_delivers_the_denial_and_does_not_execute():
    ROLLBACK_CALLS.clear()
    llm, root_rounds = _llm()
    agent = _agent(llm)

    events1 = await _collect(agent, "acc-2")
    run1 = events1[-1].run
    assert run1.state is RunState.SUSPENDED
    iv = run1.interrupt
    assert iv is not None and iv.request_id

    gate = find_approval_gate(agent)
    await gate.submit_decision(iv.request_id, ApprovalDecision.REJECT)

    events2 = await _collect(agent, "acc-2")
    run2 = events2[-1].run
    assert ROLLBACK_CALLS == [], "拒绝后工具绝不执行"
    assert run2.state is RunState.COMPLETED
    # 拒绝是一条 RED 工具结果: 模型看到它并收束（根仍只跑过一轮）
    assert root_rounds.count == 1
