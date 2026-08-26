"""合规审计 —— REACTIVE 主 agent + PLAN_FIRST 子 agent（LLM 动态 Plan + 增量重规划）。

本示例展示:
  - **LLM 动态生成 Plan** —— 子 agent 是 ``PLAN_FIRST`` 模式但**没有 hardcoded
    workflow**，LLM 在运行时根据任务和工具列表动态生成执行 Plan DAG。
  - **增量重规划** —— ``submit_to_regulator``（HIGH 副作用）执行前触发人工审批。
    人类 Reject → 步骤失败 → ``mark_downstream_obsolete`` + ``Planner.replan()``
    + ``Plan.merge()`` → 已完成的抽取/标注/关联步骤保留，被替换步骤标记 OBSOLETE，
    新步骤续跑。不推倒重来。**fallback**：被拒后不再重试 submit（避免再次弹窗被拒），
    改调只读的 ``draft_sar_for_review`` 草拟 SAR 留待合规官人工复核——对应第八章
    「传输失败 → 换协议 SCP」的「换一个动作」。
  - **幂等写** —— ``submit_to_regulator`` 标记 ``enforced_idempotent=True``，
    重试不会重复提交 SAR。

和 AIOps 一样，审批在工具级（写操作），不在 Plan 级。Plan 直接执行，只有最后的
提交动作需要人类确认。
"""

from __future__ import annotations

from prodagent import (
    Agent,
    AgentConfig,
    ExecutionMode,
    FrameworkConfig,
    HardBudget,
    LLMClient,
    use_fake_llm,
)

from compliance_audit.fake_llm import build_fake_llm
from compliance_audit.tools import (
    draft_sar_for_review,
    enrich_entity,
    extract_transactions,
    flag_suspicious,
    set_llm,
    submit_to_regulator,
)

# ── 子 agent: audit_workflow (PLAN_FIRST, 无 hardcoded workflow) ──────────

_WORKFLOW_SYSTEM = (
    "你是合规审计 agent。收到审计任务后，制定并执行审计计划：\n"
    "1. 先调用 extract_transactions 抽取待审计交易流水。\n"
    "2. 并行调用 flag_suspicious 和 enrich_entity 对交易流水做分析。\n"
    "3. 综合结果，调用 submit_to_regulator 提交 SAR 可疑活动报告。\n\n"
    "flag_suspicious 和 enrich_entity 可以并行执行（都只依赖 extract_transactions）。\n"
    "submit_to_regulator 必须等 flag_suspicious 和 enrich_entity 都完成后才能调用，"
    "且是终端步骤（terminal=true）。\n"
    "submit_to_regulator 是高危写操作，执行前会弹出人工审批——这是正常流程。\n\n"
    "## 审批被拒后的恢复（重要）\n"
    "如果 submit_to_regulator 被人类 Reject，**不要再次调用 submit_to_regulator**"
    "（那只会再次弹窗、再次被拒）。改为调用 draft_sar_for_review：复用已有的 "
    "flag_suspicious / enrich_entity 输出，整理成 SAR 草稿留待合规官人工复核。"
    "draft_sar_for_review 是只读操作（不会再弹审批），作为终端步骤（terminal=true）。"
    "这条 fallback 路径就是「增量重规划换一个动作」——已完成的抽取/标注/关联步骤全部保留，不重跑。\n\n"
    "## 工具输出结构（Plan 参数模板请使用以下字段）\n"
    "- extract_transactions → {count, transactions: [{tx_id, amount, currency, sender, receiver, timestamp, note}]}\n"
    "- flag_suspicious → {flagged: [{tx_id, reason, risk}]}\n"
    "- enrich_entity → {entities: [{name, tx_ids, total_amount, pattern}]}\n"
    "- submit_to_regulator 参数: sar_summary (str), flagged (从 flag_suspicious 输出), "
    "entities (从 enrich_entity 输出), idempotency_key (str)\n"
    "- draft_sar_for_review 参数: flagged (从 flag_suspicious 输出), "
    "entities (从 enrich_entity 输出), reason (str，可选)"
)


def build_audit_workflow_agent(
    *,
    llm: LLMClient | None = None,
    framework_config: FrameworkConfig | None = None,
) -> Agent:
    """audit_workflow 子 agent —— PLAN_FIRST 模式，无 hardcoded workflow。

    LLM 在运行时动态生成 Plan DAG。``submit_to_regulator`` 为 HIGH 副作用，
    执行前由框架 ApprovalHooks 弹窗审批。Reject 后自动触发增量重规划：不重试
    submit，改调只读的 ``draft_sar_for_review`` 草拟 SAR 转人工复核（复用已完成
    的分析步骤）。
    """
    from prodagent.base.config import production

    fw = framework_config or production()
    return Agent(
        "audit_workflow",
        system_prompt=_WORKFLOW_SYSTEM,
        tools=[
            extract_transactions,
            flag_suspicious,
            enrich_entity,
            submit_to_regulator,
            draft_sar_for_review,
        ],
        mode=ExecutionMode.PLAN_FIRST,
        budget=HardBudget(max_turns=12, max_cost_usd=0.40, max_seconds=120.0),
        config=AgentConfig(
            name="audit_workflow",
            llm=llm,
            framework=fw,
            max_replans=2,
        ),
    )


# ── 主 agent: compliance_audit (REACTIVE 对话入口) ────────────────────────

_MAIN_SYSTEM = (
    "你是合规审计编排 agent。用户想审计交易流水时，调 "
    "``spawn_agent(name=\"audit_workflow\", task=...)`` 委派给审计子 agent。\n\n"
    "## 规则\n"
    "- 用户说\"审计\"/\"查交易\"/\"有没有可疑\"时，调 spawn_agent 触发审计。"
    "不要自己逐条分析交易 —— 那是子 agent 的事。\n"
    "- 子 agent 执行到 submit_to_regulator 时会弹审批窗——这是正常流程，不是错误。\n"
    "- 审计完成后，把 SAR 结果讲给用户。\n"
    "- 用户追问某笔交易/要求重审/讨论结论时，直接对话。\n"
    "- 用户要求重新审计时，用新的 task 描述再调一次 spawn_agent。"
)

DEFAULT_TASK = "审计今日交易流水，提交可疑活动报告。"


def build_compliance_audit_agent(
    *,
    llm: LLMClient | None = None,
    framework_config: FrameworkConfig | None = None,
) -> Agent:
    """主 agent —— REACTIVE 对话入口。

    用户说"审计今天的交易" → 调 ``spawn_agent`` 委派 PLAN_FIRST 子 agent
    → 子 agent 动态出 Plan → 执行 → submit_to_regulator 弹审批 → 人类决定
    → SAR 结果返回。主 agent 永远可交互。
    """
    use_fake = use_fake_llm()
    resolved_llm = llm or (build_fake_llm() if use_fake else None)
    if resolved_llm is None:
        from prodagent.backends.factory import resolve_llm

        resolved_llm = resolve_llm(framework_config)

    set_llm(resolved_llm)

    audit_workflow = build_audit_workflow_agent(
        llm=resolved_llm,
        framework_config=framework_config,
    )

    from prodagent.core.config import production

    return Agent(
        "compliance_audit",
        system_prompt=_MAIN_SYSTEM,
        tools=[extract_transactions],
        mode=ExecutionMode.REACTIVE,
        budget=HardBudget(max_turns=20, max_cost_usd=0.60, max_seconds=180.0),
        config=AgentConfig(
            name="compliance_audit",
            llm=resolved_llm,
            framework=framework_config or production(),
            agents=[audit_workflow],
        ),
    )


__all__ = [
    "build_compliance_audit_agent",
    "build_audit_workflow_agent",
]
