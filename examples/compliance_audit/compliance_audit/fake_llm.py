"""Compliance Audit FakeLLM —— 路由机制用框架的 ``RoutingFakeLLM``。

四类 LLM 调用共享一个 FakeLLM 实例，按 system prompt 锚点分发（首匹配生效）:

  - 主 agent ``compliance_audit``       → system 含 "合规审计编排 agent"
  - Planner.generate()（动态 Plan 生成） → system 含 "RESPOND WITH JSON ONLY"
  - Planner.replan()（增量重规划）       → system 含 "incremental replanning"
  - flag_suspicious / enrich_entity     → system 含 "反洗钱分析师" / "实体关联分析师"

主 agent REACTIVE 轨迹:
  - 首次调用(无 tool result) → 发 ``spawn_agent(name="audit_workflow")``
  - 子 agent Plan 生成后 → 挂起等待人类审批
  - spawn 返回后(有 tool result) → 把 SAR 结果讲给用户
  - 用户追问 → 对话回答（不重新 spawn）
  - 用户说"重新审计"/"换个方案" → 再次 spawn

子 agent PLAN_FIRST 轨迹:
  - Planner.generate() → 返回动态 Plan JSON（s1→s2‖s3→s4）
  - Plan 生成后直接执行: s1 ✓ → s2 ✓ ‖ s3 ✓ → s4（HIGH）挂起 → 人类审批
  - 审批通过 → s4 ✓（直接执行，不重规划）
  - 审批拒绝 → Planner.replan() → 只返回 s4_v2（draft_sar_for_review，复用 s1/s2/s3，LOW 不弹审批）
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


# ── Planner.generate() 的 Plan JSON ─────────────────────────────────────────

_PLAN_JSON = (
    '{"steps": ['
    '{"id": "s1", "action": "extract_transactions", "params": {}, '
    '"depends_on": [], "terminal": false},'
    '{"id": "s2", "action": "flag_suspicious", '
    '"params": {"transactions": "{{s1.output}}"}, '
    '"depends_on": ["s1"], "terminal": false},'
    '{"id": "s3", "action": "enrich_entity", '
    '"params": {"transactions": "{{s1.output}}"}, '
    '"depends_on": ["s1"], "terminal": false},'
    '{"id": "s4", "action": "submit_to_regulator", '
    '"params": {"sar_summary": "综合可疑标注和实体关联的 SAR 报告", '
    '"suspicious_tx_ids": ["TX-1002", "TX-1003", "TX-1004"]}, '
    '"depends_on": ["s2", "s3"], "terminal": true}'
    ']}'
)

# ── Planner.replan() 的替换步骤 ─────────────────────────────────────────────
# submit_to_regulator 被人类 Reject 后，LLM 不再重试 submit（会再次弹窗被拒），
# 改调只读的 draft_sar_for_review：复用已完成的 s1/s2/s3（抽取/标注/关联），
# 草拟 SAR 留待合规官人工复核。LOW 副作用 → 不弹审批 → 直接执行完。
# 这就是「增量重规划 = 换一个动作 + 不重跑已完成步骤」：
# 对应第八章灾备迁移里「只换传输协议、复用 dump」。

_REPLAN_JSON = (
    '{"steps": ['
    '{"id": "s4_v2", "action": "draft_sar_for_review", '
    '"params": {"flagged": "{{s2.output.flagged}}", '
    '"entities": "{{s3.output.entities}}", '
    '"reason": "自动提交被审批拒绝，复用既有分析，转草拟 SAR 留待合规官人工复核"}, '
    '"depends_on": ["s2", "s3"], "terminal": true, "replaces": "s4"}'
    ']}'
)


# ── 主 agent REACTIVE 响应 ──────────────────────────────────────────────────


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
    """REACTIVE 轨迹: spawn → 总结 → 追问 → replan。"""
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
    """离线 demo 用: 主 agent + Planner + s2/s3 工具 LLM 调用全 scripted。"""
    flag_q: list[LLMResponse] = []
    entity_q: list[LLMResponse] = []
    replan_calls = 0

    def _route_flag(_messages: MessageList) -> LLMResponse:
        return flag_q.pop(0) if flag_q else _FLAG_RESPONSE

    def _route_entity(_messages: MessageList) -> LLMResponse:
        return entity_q.pop(0) if entity_q else _ENTITY_RESPONSE

    def _route_planner_generate(_messages: MessageList) -> LLMResponse:
        return LLMResponse(
            content=_PLAN_JSON,
            stop_reason="end_turn",
            input_tokens=100,
            output_tokens=80,
        )

    def _route_planner_replan(_messages: MessageList) -> LLMResponse:
        nonlocal replan_calls
        replan_calls += 1
        if replan_calls > 2:
            # 超过 max_replans，返回空步骤 → Scheduler 停止
            return LLMResponse(
                content='{"steps": []}',
                stop_reason="end_turn",
                input_tokens=50,
                output_tokens=10,
            )
        return LLMResponse(
            content=_REPLAN_JSON,
            stop_reason="end_turn",
            input_tokens=80,
            output_tokens=60,
        )

    return RoutingFakeLLM(
        routes={
            # 注册顺序即路由优先级（首匹配生效），镜像原来的 elif 链
            "合规审计编排 agent": [_route_main_agent],
            "RESPOND WITH JSON ONLY": [_route_planner_generate],
            "incremental replanning": [_route_planner_replan],
            "反洗钱分析师": [_route_flag],
            "实体关联分析师": [_route_entity],
        }
    )
