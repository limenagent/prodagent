"""Compliance Audit FakeLLM —— 按 system prompt 路由到 per-agent 队列。

三类 agent 共享一个 FakeLLM 实例,按 system prompt 内容分发:

  - 主 agent ``compliance_audit``   → system 含 "合规审计编排 agent"
  - s2/s3 llm_step                  → system 含 "反洗钱分析师" / "实体关联分析师"
  - s4 ``sar_submitter``            → system 含 "SAR 提交 agent"

主 agent REACTIVE 轨迹:
  - 首次调用(无 tool result) → 发 ``spawn_agent(name="audit_workflow")``
  - spawn 返回后(有 tool result) → 把 SAR 结果讲给用户

s2/s3 的 ``wf.llm_step`` 并行执行,调用顺序不定;线性 ``script()`` 没法处理
并行顺序,所以用 RoutingFakeLLM 按 system prompt 分发。

poison 装填时(``reset_crash_state(poison=True)``),s3 的 LLM 调用直接抛
RuntimeError —— 模拟 "进程被杀",run FAILED。续跑时 poison 解除,LLM 正常
返回,s1/s2 的 COMPLETED 留在 event log 跳过,只重跑 s3/s4。
"""

from __future__ import annotations

import asyncio
from typing import Any

from prodagent.core.types import LLMResponse, MessageList, ToolCall
from prodagent.llm.base import LLMClient, LLMConfig

# poison 装填时,s3 的 LLM 调用抛 RuntimeError —— 模拟进程被杀。
_POISON_ACTIVE: bool = False


def reset_crash_state(*, poison: bool = False) -> None:
    """设定 s3 llm_step 的 LLM 调用是否崩溃。

    demo 每次 run 前调一次: RUN 1 让 poison 触发,RUN 2/3 关掉。
    """
    global _POISON_ACTIVE
    _POISON_ACTIVE = poison


_FLAG_RESPONSE = LLMResponse(
    content=(
        '{"flagged": ['
        '{"tx_id": "TX-1002", "reason": "大额汇至壳公司 shell-co-122-cayman", "risk": "high"},'
        '{"tx_id": "TX-1003", "reason": "9800 美元兑加密货币，接近 10000 申报阈值", "risk": "medium"},'
        '{"tx_id": "TX-1004", "reason": "12 万美元二次汇至同一壳公司，疑似拆分规避", "risk": "high"}'
        ']}'
    ),
    stop_reason="end_turn",
    input_tokens=50,
    output_tokens=40,
)

_ENTITY_RESPONSE = LLMResponse(
    content=(
        '{"entities": ['
        '{"name": "shell-co-122-cayman", "tx_ids": ["TX-1002", "TX-1004"], '
        '"total_amount": 168500.00, "pattern": "同一壳公司两次大额收款，间隔 7 分钟"},'
        '{"name": "crypto-exchange-X", "tx_ids": ["TX-1003"], '
        '"total_amount": 9800.00, "pattern": "逼近 10000 申报阈值的加密兑换"}'
        ']}'
    ),
    stop_reason="end_turn",
    input_tokens=50,
    output_tokens=40,
)


def _submit_tool_call() -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(
            name="submit_to_regulator",
            params={
                "sar_summary": "两笔大额（48500 + 120000 USD）汇往壳公司 shell-co-122-cayman，"
                               "间隔 7 分钟，疑似拆分规避申报；另有一笔 9800 USD 兑加密货币逼近阈值。",
                "suspicious_tx_ids": ["TX-1002", "TX-1003", "TX-1004"],
            },
        )],
        stop_reason="tool_use",
    )


_SUBMIT_SUMMARY = LLMResponse(
    content="SAR 已提交。3 笔可疑交易上报监管，含同一壳公司两次大额收款的结构性拆分模式。",
    stop_reason="end_turn",
)


def _spawn_audit_workflow_call() -> LLMResponse:
    """主 agent 首次调用 → 委派给 audit_workflow 子 agent。"""
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(
            name="spawn_agent",
            params={
                "name": "audit_workflow",
                "task": "审计今日交易流水：抽取交易 → 标注可疑 ‖ 关联实体 → 提交 SAR。",
            },
        )],
        stop_reason="tool_use",
    )


def _main_agent_summary() -> LLMResponse:
    """主 agent 拿到 spawn 结果后 → 把 SAR 结论讲给用户。"""
    return LLMResponse(
        content=(
            "审计完成。SAR 已提交监管,3 笔可疑交易:\n"
            "- TX-1002 + TX-1004: 两笔大额(48500 + 120000 USD)汇往同一壳公司 "
            "shell-co-122-cayman,间隔 7 分钟,典型拆分规避申报。\n"
            "- TX-1003: 9800 USD 兑加密货币,逼近 10000 申报阈值。\n\n"
            "你可以追问某笔交易的细节,或要求重新审计新一批交易。"
        ),
        stop_reason="end_turn",
    )


def _followup_response(user_msg: str) -> LLMResponse:
    """主 agent 被追问 → 基于已有审计结果对话回答,不重新跑 DAG。"""
    if "TX-1002" in user_msg:
        content = (
            "TX-1002: 48500 USD 从 finance@corp.com 汇往壳公司 shell-co-122-cayman,"
            "标注为 'consulting fee — urgent'。与 TX-1004(12 万 USD)间隔 7 分钟"
            "汇往同一壳公司,典型拆分规避申报阈值(1 万/10 万 USD)的结构性拆分模式。"
            "已纳入 SAR 上报。"
        )
    elif "重审" in user_msg or or_match(user_msg):
        content = "好的,我重新触发审计 workflow 扫描最新交易。"
    else:
        content = (
            "基于刚才的审计结果,3 笔可疑交易已上报。你想了解哪笔交易的细节?"
        )
    return LLMResponse(content=content, stop_reason="end_turn", input_tokens=50, output_tokens=30)


def or_match(msg: str) -> bool:
    return "重新审计" in msg or "新一批" in msg


def last_user_of(messages: MessageList) -> str:
    return next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"),
        "(no user message)",
    )


class ComplianceFakeLLM(LLMClient):
    """按 system prompt 把 complete() 分发到 per-agent 队列。"""

    def __init__(self) -> None:
        self._routes: list[tuple[str, list[LLMResponse]]] = [
            ("反洗钱分析师", [_FLAG_RESPONSE]),
            ("实体关联分析师", [_ENTITY_RESPONSE]),
        ]
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str | list[dict[str, Any]] = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: Any = None,
    ) -> LLMResponse:
        self._call_count += 1
        sys_str = system if isinstance(system, str) else str(system)

        # s2/s3 llm_step: 按 system marker 取队列。
        for marker, q in self._routes:
            if marker in sys_str and q:
                if _POISON_ACTIVE and marker == "实体关联分析师":
                    # poison 触发: 模拟 s3 llm_step 的 LLM 调用崩了（进程被杀），
                    # step FAILED → run FAILED，续跑时从 s3 重跑（跳过已完成的 s1/s2）。
                    raise RuntimeError(
                        "simulated crash in s3 enrich_entity LLM call —— "
                        "杀掉进程，然后用同一个 run_id 重跑来续跑。"
                    )
                resp = q.pop(0)
                break
        else:
            # s4 sar_submitter: REACTIVE 轨迹，看 tool result 决定下一步。
            if "SAR 提交 agent" in sys_str:
                if any(m.get("role") == "tool" for m in messages):
                    resp = _SUBMIT_SUMMARY
                else:
                    resp = _submit_tool_call()
            elif "合规审计编排 agent" in sys_str:
                # 主 agent REACTIVE 轨迹:
                # - 最后一条非 assistant 消息是 user(无 tool result 在其后) → 触发 spawn
                # - 刚 spawn 返回(最后一条非 assistant 是 tool) → 总结 SAR 结果
                # - 用户追问(有 tool result 但最后一条非 assistant 是 user) → 对话回答
                last_non_assistant = next(
                    (m for m in reversed(messages) if m.get("role") != "assistant"),
                    None,
                )
                if last_non_assistant is not None and last_non_assistant.get("role") == "tool":
                    resp = _main_agent_summary()
                else:
                    # 无 tool result(首轮)或用户追问(有历史 tool result 但本轮是 user 消息)
                    if any(m.get("role") == "tool" for m in messages):
                        # 有历史 spawn 结果,但本轮是用户追问 → 对话回答
                        resp = _followup_response(last_user_of(messages))
                    else:
                        resp = _spawn_audit_workflow_call()
            else:
                last_user = next(
                    (m["content"] for m in reversed(messages) if m.get("role") == "user"),
                    "(no user message)",
                )
                resp = LLMResponse(
                    content=f"[fallback] {last_user}", stop_reason="end_turn",
                    input_tokens=50, output_tokens=10,
                )

        if resp.content and on_chunk is not None:
            for word in resp.content.split():
                await on_chunk(word + " ")
                await asyncio.sleep(0)
        return resp


def build_fake_llm() -> LLMClient:
    """离线 demo 用: 主 agent + s2/s3 llm_step + s4 子 agent 全 scripted。"""
    return ComplianceFakeLLM()
