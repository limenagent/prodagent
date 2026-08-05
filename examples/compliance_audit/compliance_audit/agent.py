"""合规审计 —— 主 agent REACTIVE 多轮对话 + workflow 子 agent + 崩溃恢复。

本示例展示:
  - **主 agent REACTIVE** —— ``compliance_audit`` 主 agent 是对话入口,
    REACTIVE 模式。用户说"审计今天的交易" → 主 agent 调 ``spawn_agent``
    委派给 ``audit_workflow`` 子 agent 跑固定 DAG → 拿到 SAR 结果后
    继续对话(追问某笔交易、要求重审、讨论合规结论)。DAG 跑完不阻塞
    对话 —— 主 agent 永远可交互。
  - **workflow 子 agent** —— ``audit_workflow`` 是 ``workflow=`` 构造的
    固定 DAG(extract → flag ‖ enrich → submit_sar),通过 ``spawn_agent``
    触发。DAG 跑完返回 SAR 结果给主 agent。子 agent 是固定流程,主 agent
    是灵活对话 —— 两者职责分离。
  - **崩溃恢复** —— poison pill 在 s3 的 llm_step 里触发(模拟 LLM 调用
    中途进程被杀),s1/s2 的 COMPLETED 留在 event log,续跑时跳过 —— 省
    下的 LLM 调用成本可见。
  - **幂等写工具** —— ``submit_to_regulator`` 标为
    ``enforced_idempotent=True``,崩溃重试不会重复提交 SAR。

为什么主 agent REACTIVE + workflow 子 agent: 合规审计是对话场景 ——
用户会追问"TX-1002 再深挖"、"换个阈值重审"、"上次报告再确认一下"。
固定 DAG 跑完就死,不能对话;REACTIVE 主 agent 永远在,按需触发 DAG。
"""

from __future__ import annotations

import os

from prodagent import Agent, ExecutionMode, HardBudget
from prodagent.core.config import FrameworkConfig
from prodagent.llm.base import LLMClient

from compliance_audit.fake_llm import build_fake_llm, reset_crash_state
from compliance_audit.tools import extract_transactions, submit_to_regulator
from compliance_audit.workflow import build_audit_workflow


def build_sar_submitter(llm: LLMClient | None = None) -> Agent:
    """s4 子 agent —— 综合两份 LLM 分析后调 submit_to_regulator 提交 SAR。

    REACTIVE 模式: 拿到 task(含 s2/s3 标注)→ LLM 综合 → 调 HIGH 副作用写工具。
    poison pill 在 ``submit_to_regulator`` 里: RUN 1 崩,续跑时解除。
    """
    return Agent(
        "sar_submitter",
        system_prompt=(
            "你是 SAR 提交 agent。拿到上游两份 LLM 分析(可疑标注 + 实体关联),"
            "综合成一段 sar_summary 并调 submit_to_regulator 提交 SAR 报告。"
            "suspicious_tx_ids 填所有 risk=medium/high 的 tx_id。"
            "submit_to_regulator 幂等 —— 崩溃重试不会重复提交。"
        ),
        tools=[submit_to_regulator],
        llm=llm,
        description="综合可疑标注与实体关联,提交 SAR 报告。",
        mode=ExecutionMode.REACTIVE,
        budget=HardBudget(max_turns=3, max_cost_usd=0.20, max_seconds=60.0),
    )


def build_audit_workflow_agent(
    *,
    llm: LLMClient | None = None,
    framework_config: FrameworkConfig | None = None,
) -> Agent:
    """audit_workflow 子 agent —— 固定 DAG(extract → flag ‖ enrich → submit_sar)。

    ``workflow=`` 构造,DAG 写死跳过 LLM planning。s2/s3 是真 LLM 标注,
    s4 委派给 sar_submitter 子 agent。通过主 agent 的 ``spawn_agent`` 触发。
    """
    sar_submitter = build_sar_submitter(llm)
    wf = build_audit_workflow(sar_submitter)
    return Agent(
        "audit_workflow",
        system_prompt=(
            "你是合规审计 workflow agent。DAG 写死: "
            "extract_transactions → flag_suspicious ‖ enrich_entity → submit_sar。"
            "你不需要生成 plan —— 直接执行。"
        ),
        tools=[extract_transactions],
        llm=llm,
        framework=framework_config,
        workflow=wf,
        allow_replan=False,
        agents=[sar_submitter],
        budget=HardBudget(max_turns=12, max_cost_usd=0.40, max_seconds=120.0),
    )


DEFAULT_TASK = "审计今日交易流水，提交可疑活动报告。"


def build_compliance_audit_agent(
    *,
    llm: LLMClient | None = None,
    framework_config: FrameworkConfig | None = None,
) -> Agent:
    """主 agent —— REACTIVE 对话入口。

    用户说"审计今天的交易" → 主 agent 调 ``spawn_agent(name="audit_workflow")``
    委派固定 DAG → 拿到 SAR 结果后继续对话。主 agent 永远可交互,DAG 跑完
    不阻塞对话。

    Args:
        llm: 主 agent 用的 LLM。不传时按 USE_FAKE_LLM 决定。
        framework_config: 父 fw;不传时用 default。playground 注入带独立 namespace 的 fw。
    """
    use_fake = os.getenv("USE_FAKE_LLM", "").lower() in ("1", "true", "yes")
    resolved_llm = llm or (build_fake_llm() if use_fake else None)
    audit_workflow = build_audit_workflow_agent(
        llm=resolved_llm,
        framework_config=framework_config,
    )

    return Agent(
        "compliance_audit",
        system_prompt=(
            "你是合规审计编排 agent。用户想审计交易流水时,调 "
            "``spawn_agent(name=\"audit_workflow\", task=...)`` 委派给固定的 "
            "审计 workflow(extract → flag ‖ enrich → submit_sar)。"
            "workflow 跑完会返回 SAR 提交结果,你把结果讲给用户。\n\n"
            "## 规则\n"
            "- 用户说\"审计\"/\"查交易\"/\"有没有可疑\"时,调 spawn_agent 触发 DAG。"
            "不要自己逐条分析交易 —— 那是 workflow 的事。\n"
            "- DAG 跑完,把 SAR 结果(提交了哪些可疑交易、为什么)讲给用户。\n"
            "- 用户追问某笔交易/要求重审/讨论结论时,直接对话 —— 不需要再跑 DAG。\n"
            "- 用户要重新审计(新一批交易)时,再调一次 spawn_agent。"
        ),
        tools=[extract_transactions],
        llm=resolved_llm,
        framework=framework_config,
        mode=ExecutionMode.REACTIVE,
        agents=[audit_workflow],
        budget=HardBudget(max_turns=20, max_cost_usd=0.60, max_seconds=180.0),
    )


__all__ = [
    "build_compliance_audit_agent",
    "build_audit_workflow_agent",
    "build_sar_submitter",
    "reset_crash_state",
]
