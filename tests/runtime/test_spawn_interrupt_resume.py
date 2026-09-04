"""验收（2026-09-04 事故回放）: spawn 子 agent 的审批挂起必须收敛到统一的
interrupt 机制，父 turn 不得越过未决审批自行 COMPLETED。

事故形态: 父 spawn → 子 agent 的 submit（HIGH）挂在审批门 → spawn 门把
ChildResult(suspended) asdict 成普通 dict 返回 —— 挂起降格为数据，三条单
咽喉（dispatcher park / bootstrap restore / staged verbatim 重放）全部看不
见它，父 run 告诉用户"请在审批窗口确认"后照常 COMPLETED，Web UI 永远等不
到 RunSuspendedEvent，审批框弹不出来。

修复后的定律:
- spawn 门把子挂起翻译回唯一形状 ToolResult.suspended（同 _fold_child）；
- 父 park 在 spawn 调用上（staged call = spawn_agent 本身），interrupt 携
  带子门铸的 request_id —— UI 弹框的就是它；
- 批准经同一 ApprovalGate 实例（fork 共享父 hooks）落 _deferred，恢复时父
  的 staged spawn 重放 → 同一子 run id → 子 checkpoint 过唯一 restore 咽喉
  → 子的 staged submit verbatim 重放 → 批准后恰执行一次。
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
from prodagent.hooks.bundles.security.approval import ApprovalHooks
from prodagent.kernel.interrupt import InterruptKind
from prodagent.kernel.types import LLMResponse, ToolCall
from prodagent.runtime.runner import drive_stream, find_approval_gate
from prodagent.tooling import tool

SUBMIT_CALLS: list[dict] = []


@tool(
    name="submit_report",
    meta=ToolMeta(name="submit_report", side_effect_level=SideEffectLevel.HIGH),
)
async def submit_report(summary: str) -> dict:
    SUBMIT_CALLS.append({"summary": summary})
    return {"submitted": True, "summary": summary}


def _llm(
    *, auditor_tail: list[LLMResponse], orchestrator_tail: list[LLMResponse]
) -> RoutingFakeLLM:
    llm = RoutingFakeLLM()
    llm.add(
        "orchestrator",
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        name="spawn_agent",
                        params={"name": "auditor", "task": "审计今日交易流水"},
                        call_id="s1",
                    )
                ],
                stop_reason="tool_use",
            ),
            *orchestrator_tail,
        ],
    )
    llm.add(
        "auditor",
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        name="submit_report",
                        params={"summary": "3 笔可疑交易"},
                        call_id="a1",
                    )
                ],
                stop_reason="tool_use",
            ),
            *auditor_tail,
        ],
    )
    return llm


def _agent(llm: RoutingFakeLLM) -> Agent:
    # 两段 drive 共享同一 checkpoint store 与同一 ApprovalHooks —— fork 从
    # 父的活 hooks 里拿同一个 gate 实例，决定落谁家一目了然。
    approval = ApprovalHooks()
    checkpoint = in_memory_checkpoint_store()
    auditor = Agent(
        "auditor",
        tools=[submit_report],
        system_prompt="You audit transactions and submit reports.",
        budget=HardBudget(max_turns=8),
        config=AgentConfig(name="auditor", llm=llm, extensions=[approval], checkpoint=checkpoint),
    )
    return Agent(
        "orchestrator",
        tools=[],
        system_prompt="You orchestrate audits.",
        budget=HardBudget(max_turns=8),
        config=AgentConfig(
            name="orchestrator",
            llm=llm,
            extensions=[approval],
            checkpoint=checkpoint,
            agents=[auditor],
        ),
    )


async def _collect(agent: Agent, run_id: str):
    events = []
    async for event in drive_stream(agent, "审计今日交易流水。", run_id=run_id):
        events.append(event)
    return events


@pytest.mark.asyncio
async def test_spawn_child_approval_parks_the_parent_and_resumes_through_it():
    SUBMIT_CALLS.clear()
    llm = _llm(
        auditor_tail=[LLMResponse(content="report submitted", stop_reason="end_turn")],
        orchestrator_tail=[
            LLMResponse(content="SAR submitted to regulator", stop_reason="end_turn")
        ],
    )
    agent = _agent(llm)

    # ── 第一段: spawn → 子的 submit_report 挂在审批门 ──
    events1 = await _collect(agent, "spawn-acc-1")
    run1 = events1[-1].run
    assert run1.state is RunState.SUSPENDED, "父必须 park 在未决审批上，不得 COMPLETED"
    iv = run1.interrupt
    assert iv is not None and iv.kind is InterruptKind.APPROVE
    # stage 的是 spawn 调用本身 —— 恢复时重放它，子 checkpoint 接力
    staged = iv.staged_call()
    assert staged is not None and staged.name == "spawn_agent"
    assert iv.request_id, "interrupt 必须携带子门铸的 request id（UI 弹框靠它）"
    assert SUBMIT_CALLS == [], "批准前 HIGH 工具绝不落地"

    # ── 批准（经父 agent 的门 —— fork 共享同一实例）──
    gate = find_approval_gate(agent)
    assert gate is not None
    await gate.submit_decision(iv.request_id, ApprovalDecision.APPROVE)

    # ── 第二段: 恢复 —— spawn verbatim 重放 → 子恢复 → submit 恰执行一次 ──
    events2 = await _collect(agent, "spawn-acc-1")
    run2 = events2[-1].run
    assert run2.state is RunState.COMPLETED
    assert len(SUBMIT_CALLS) == 1 and SUBMIT_CALLS[0]["summary"] == "3 笔可疑交易", (
        "submit 恰好执行一次，且在批准之后"
    )
    assert run2.final_output is not None


@pytest.mark.asyncio
async def test_spawn_child_reject_keeps_the_tool_unexecuted_and_the_turn_alive():
    SUBMIT_CALLS.clear()
    # 拒绝是 RED 工具结果回喂子循环 —— 子模型收束为最终答案，父汇报结果
    llm = _llm(
        auditor_tail=[LLMResponse(content="rejected; drafted for review", stop_reason="end_turn")],
        orchestrator_tail=[
            LLMResponse(content="SAR rejected; draft filed for review", stop_reason="end_turn")
        ],
    )
    agent = _agent(llm)

    events1 = await _collect(agent, "spawn-acc-2")
    run1 = events1[-1].run
    assert run1.state is RunState.SUSPENDED
    iv = run1.interrupt
    assert iv is not None and iv.request_id

    gate = find_approval_gate(agent)
    await gate.submit_decision(iv.request_id, ApprovalDecision.REJECT)

    events2 = await _collect(agent, "spawn-acc-2")
    run2 = events2[-1].run
    assert SUBMIT_CALLS == [], "拒绝后工具绝不执行"
    assert run2.state is RunState.COMPLETED
