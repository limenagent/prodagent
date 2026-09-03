"""Compliance Audit FakeLLM —— 路由机制用框架的 ``RoutingFakeLLM``。

主 agent（对话入口）委派 spawn；子 agent（plan-and-resolve）两个
节点：``plan`` 节点产审计目标清单文本（数据，不是执行图），``work`` 节点
跑 think-act 循环执行清单。s2/s3 工具 LLM 调用全 scripted。

四类 LLM 调用共享一个 FakeLLM 实例，按 system prompt 锚点分发（首匹配生效）:

  - 主 agent ``compliance_audit``       → system 含 "合规审计编排 agent"
  - flag_suspicious / enrich_entity     → system 含 "反洗钱分析师" / "实体关联分析师"

主 agent 轨迹:
  - 首次调用(无 tool result) → 发 ``spawn_agent(name="audit_workflow")``
  - 子 agent Plan 生成后 → 挂起等待人类审批
  - spawn 返回后(有 tool result) → 把 SAR 结果讲给用户
  - 用户追问 → 对话回答（不重新 spawn）
  - 用户说"重新审计"/"换个方案" → 再次 spawn

子 agent plan-and-resolve 轨迹:
  - plan 节点产审计目标清单文本 → work 节点（ReAct）执行: extract → flag‖enrich → submit
  - submit_to_regulator（HIGH）挂起 → 人类审批
  - 审批通过 → submit 执行完
  - 审批拒绝 → 改调 draft_sar_for_review（只读，不弹审批）草稿复核
"""

from __future__ import annotations

from prodagent import LLMClient, RoutingFakeLLM
from prodagent.kernel.types import LLMResponse, MessageList, ToolCall

# ── flag_suspicious / enrich_entity 的 canned LLM 响应 ───────────────────────

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



# ── 主 agent 响应 ─────────────────────────────────────────────────────────


def _spawn_audit_workflow_call() -> LLMResponse:
    """主 agent 首次调用 → 委派给 audit_workflow 子 agent。"""
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(
            name="spawn_agent",
            params={
                "name": "audit_workflow",
                "task": "审计今日交易流水：抽取交易 → 标注可疑 ‖ 关联实体 → 提交 SAR 报告。",
            },
        )],
        stop_reason="tool_use",
    )


def _main_agent_summary() -> LLMResponse:
    """主 agent 拿到 spawn 结果后 → 把 SAR 结论讲给用户。"""
    return LLMResponse(
        content=(
            "审计完成。SAR 已提交监管，3 笔可疑交易:\n"
            "- TX-1002 + TX-1004: 两笔大额(48500 + 120000 USD)汇往同一壳公司 "
            "shell-co-122-cayman，间隔 7 分钟，典型拆分规避申报。\n"
            "- TX-1003: 9800 USD 兑加密货币，逼近 10000 申报阈值。\n\n"
            "你可以追问某笔交易的细节，或说'换个方案重新审计'触发重新审计。"
        ),
        stop_reason="end_turn",
    )


def _followup_response(user_msg: str) -> LLMResponse:
    """主 agent 被追问 → 基于已有审计结果对话回答，不重新跑 Plan。"""
    if "TX-1002" in user_msg:
        content = (
            "TX-1002: 48500 USD 从 finance@corp.com 汇往壳公司 shell-co-122-cayman，"
            "标注为 'consulting fee — urgent'。与 TX-1004(12 万 USD)间隔 7 分钟"
            "汇往同一壳公司，典型拆分规避申报阈值(1 万/10 万 USD)的结构性拆分模式。"
            "已纳入 SAR 上报。"
        )
    elif "重审" in user_msg or _is_replan(user_msg):
        content = "好的，我重新触发审计，用新的方案重新生成执行计划。"
    else:
        content = (
            "基于刚才的审计结果，3 笔可疑交易已上报。你想了解哪笔交易的细节？"
        )
    return LLMResponse(content=content, stop_reason="end_turn", input_tokens=50, output_tokens=30)


def _is_replan(msg: str) -> bool:
    """检测用户是否触发了 replan 意图。"""
    return any(kw in msg for kw in ("重新审计", "新一批", "换个方案", "换种方式", "重试"))


def _last_user_of(messages: MessageList) -> str:
    return next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"),
        "(no user message)",
    )


def _route_main_agent(messages: MessageList) -> LLMResponse:
    """主 agent 轨迹: spawn → 总结 → 追问。"""
    last_non_assistant = next(
        (m for m in reversed(messages) if m.get("role") != "assistant"),
        None,
    )
    if last_non_assistant is not None and last_non_assistant.get("role") == "tool":
        # 子 agent spawn 返回了 → 总结 SAR 结果
        return _main_agent_summary()

    # 无 tool result（首轮）或用户追问
    if any(m.get("role") == "tool" for m in messages):
        # 有历史 spawn 结果，但本轮是用户追问
        user_msg = _last_user_of(messages)
        return _followup_response(user_msg)

    # 首轮: 触发 spawn
    return _spawn_audit_workflow_call()


def build_fake_llm() -> LLMClient:
    """离线 demo 用: 主 agent + plan/work 两节点 + s2/s3 工具 LLM 调用全 scripted。"""
    flag_q: list[LLMResponse] = []
    entity_q: list[LLMResponse] = []

    def _route_flag(_messages: MessageList) -> LLMResponse:
        return flag_q.pop(0) if flag_q else _FLAG_RESPONSE

    def _route_entity(_messages: MessageList) -> LLMResponse:
        return entity_q.pop(0) if entity_q else _ENTITY_RESPONSE

    def _route_plan_node(_messages: MessageList) -> LLMResponse:
        """The plan node (one fixed-prompt LLM call): its output is the
        worker's goal — task-list DATA, not a graph (column 24)."""
        return LLMResponse(
            content=(
                "审计目标清单：\n"
                "1. 调用 extract_transactions 抽取今日交易流水。\n"
                "2. 调用 flag_suspicious 标注可疑交易，调用 enrich_entity 关联实体。\n"
                "3. 综合结果调用 submit_to_regulator 提交 SAR 可疑活动报告"
                "（高危写操作，执行前有人工审批；若被拒绝，改调 "
                "draft_sar_for_review 草拟 SAR 留待人工复核）。"
            ),
            stop_reason="end_turn",
            input_tokens=80,
            output_tokens=90,
        )

    def _route_worker(messages: MessageList) -> LLMResponse:
        """The work node (a ReAct loop over the five tools). Rounds advance
        by what has already come back: extract → flag+enrich → submit (the
        HIGH one parks at approval) → final summary."""
        got = {m.get("role") for m in messages}
        has_tool = any(m.get("role") == "tool" for m in messages)
        # count executed tools by scanning tool messages for call names
        done: set[str] = set()
        for m in messages:
            if m.get("role") == "tool":
                name = m.get("name") or m.get("tool_call_id") or ""
                done.add(str(name))
        tool_names = "extract_transactions flag_suspicious enrich_entity submit_to_regulator draft_sar_for_review"
        del tool_names
        if not has_tool:
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(
                    name="extract_transactions",
                    params={},
                )],
                stop_reason="tool_use",
                input_tokens=200,
                output_tokens=20,
            )
        if "extract_transactions" not in done and "plan_extract_transactions" not in done:
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(name="extract_transactions", params={})],
                stop_reason="tool_use",
                input_tokens=200,
                output_tokens=20,
            )
        if not ({"flag_suspicious", "enrich_entity"} & done):
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(name="flag_suspicious", params={}),
                    ToolCall(name="enrich_entity", params={}),
                ],
                stop_reason="tool_use",
                input_tokens=400,
                output_tokens=40,
            )
        if "submit_to_regulator" not in done and "draft_sar_for_review" not in done:
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(
                    name="submit_to_regulator",
                    params={
                        "sar_summary": "综合可疑标注和实体关联的 SAR 报告",
                        "suspicious_tx_ids": ["TX-1002", "TX-1003", "TX-1004"],
                    },
                )],
                stop_reason="tool_use",
                input_tokens=600,
                output_tokens=60,
            )
        # submit came back (approved or rejected-as-fallback) — finish
        return LLMResponse(
            content=(
                "审计完成：3 笔可疑交易（TX-1002/TX-1004 同一壳公司拆分汇款、"
                "TX-1003 逼近申报阈值的加密兑换），SAR 报告已按流程处理。"
            ),
            stop_reason="end_turn",
            input_tokens=800,
            output_tokens=80,
        )

    return RoutingFakeLLM(
        routes={
            # 注册顺序即路由优先级（首匹配生效）
            "合规审计编排 agent": [_route_main_agent],
            "你是合规审计 agent": [_route_plan_node],
            "# audit_workflow Agent": [_route_worker],
            "反洗钱分析师": [_route_flag],
            "实体关联分析师": [_route_entity],
        }
    )
