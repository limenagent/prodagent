"""子 Agent —— 诊断 fan-out + remediator（peer）。

三个只读诊断专家（investigator 并行 fan-out spawn），加一个 remediator
—— 作为 investigator 的 **peer**：investigator 调 ``handoff_to_remediator``
结束自己的 run，remediator 接过 IncidentReport 继续跑。和 spawn 不同，
peer 模式下 remediator 不共享父 LLM，``_build_peer_agent`` 复制
``spec.llm`` —— 所以 ``remediator_agent(llm=...)`` 必须显式传 LLM。
"""

from __future__ import annotations

from prodagent import Agent, AgentConfig, ExecutionMode, HardBudget, LLMClient

from aiops.tools import (
    check_slo,
    get_pod_status,
    get_pr_diff,
    get_recent_deploys,
    open_incident,
    page_oncall,
    query_metrics,
    restart_pod,
    rollback,
    tail_logs,
    update_incident,
)


def log_analysis_agent() -> Agent:
    """只读日志专家 —— REACTIVE，让并行 gather 能跑。"""
    return Agent(
        "log_analysis",
        system_prompt=(
            "用 tail_logs 拉故障服务的日志（如果涉及某个 pod，也调 "
            "get_pod_status）。报告主要的错误签名和 pod；如果日志为空，"
            "报告 '无有效信号'。"
        ),
        tools=[tail_logs, get_pod_status],
        mode=ExecutionMode.REACTIVE,
        budget=HardBudget(max_seconds=600),
        config=AgentConfig(
            name="log_analysis",
            description="只读日志专家。提取错误签名（OOMKill、panic、堆栈）。",
        ),
    )


def deploy_correlation_agent() -> Agent:
    """只读部署专家 —— REACTIVE。"""
    return Agent(
        "deploy_correlation",
        system_prompt=(
            "用 get_recent_deploys（对任何可疑 SHA 调 get_pr_diff）判断"
            "近期部署是否与故障相关。如果是: 报告可疑 SHA 和要回滚到的"
            "上一个好 SHA。如果不是: 报告 '无相关部署'。"
        ),
        tools=[get_recent_deploys, get_pr_diff],
        mode=ExecutionMode.REACTIVE,
        budget=HardBudget(max_seconds=600),
        config=AgentConfig(
            name="deploy_correlation",
            description="只读部署专家。判断某次代码变更是否可能导致了故障。",
        ),
    )


def metric_anomaly_agent() -> Agent:
    """只读指标专家 —— REACTIVE。"""
    return Agent(
        "metric_anomaly",
        system_prompt=(
            "用 query_metrics 量化异常和 SLO burn rate。以数字形式报告"
            " burn rate 和驱动的指标；如果一切正常，报告 '指标正常'。"
        ),
        tools=[query_metrics],
        mode=ExecutionMode.REACTIVE,
        budget=HardBudget(max_seconds=600),
        config=AgentConfig(
            name="metric_anomaly",
            description="只读指标专家。量化 SLO burn rate 和异常信号。",
        ),
    )


def remediator_agent(*, llm: LLMClient | None = None) -> Agent:
    """修复专家 —— investigator 的 peer，接 IncidentReport 继续。

    peer 模式下 ``_build_peer_agent`` 复制 ``spec.llm`` —— 不像 spawn
    那样继承父 LLM。fake 模式必须显式传一个能路由到 remediator 队列的
    ``RoutingFakeLLM``；真 LLM 模式传 ``create_llm_client()``。
    """
    return Agent(
        "remediator",
        system_prompt=(
            "你为已确认的故障执行修复 playbook。\n\n"
            "输入是 investigator 的 IncidentReport（作为 prior agent "
            "output 附在任务里）。始终先 open_incident，在任何 HIGH "
            "工具前用 update_incident 记录。\n\n"
            "决策树:\n"
            "  - 与部署相关（suspicious_sha != N/A）: update_incident 写"
            "根因 + 回滚目标，然后 rollback（HIGH），再用 "
            "get_pod_status + check_slo 验证，最后 update_incident 写"
            "postmortem。\n"
            "  - OOM / CrashLoopBackOff 且与部署无关: update_incident 写"
            "计划，然后 restart_pod（HIGH），再 get_pod_status + "
            "check_slo，最后 update_incident 写 postmortem。\n"
            "  - 根因不清: open_incident 然后 page_oncall。\n\n"
            "状态纪律: rollback 或 restart_pod 后，status 设为 "
            "'mitigated'（不是 'resolved'）。只有在后续 check_slo 显示 "
            "error_budget_remaining_pct >= 0 后才设 'resolved'。\n\n"
            "{{step_X.output.<key>}} 引用可用的工具输出键: "
            "open_incident→{incident_id,title,severity}; "
            "update_incident→{incident_id,status,updated,next_steps}; "
            "rollback→{service,rolled_back_to,status}; "
            "restart_pod→{pod,restarted,status}; "
            "get_pod_status→{service,replicas,pods}; "
            "check_slo→{service,error_budget_remaining_pct,burn_rate_1h,burn_rate_6h,alert_fired,status}; "
            "page_oncall→{team,paged,severity}."
        ),
        tools=[
            open_incident,
            update_incident,
            rollback,
            restart_pod,
            get_pod_status,
            check_slo,
            page_oncall,
        ],
        mode=ExecutionMode.PLAN_FIRST,
        budget=HardBudget(max_seconds=600),
        config=AgentConfig(
            name="remediator",
            llm=llm,
            description=(
                "修复专家。investigator 把根因明确的 IncidentReport 交给它 —— "
                "handoff_to_remediator 结束 investigator 的 run，remediator 接"
                "过 report 继续。根因为 'Unknown' 时，investigator 直接 "
                "page_oncall，不 handoff。"
            ),
        ),
    )


def diagnostic_child_agents() -> list[Agent]:
    """investigator fan-out 的三个只读诊断 agent。"""
    return [
        log_analysis_agent(),
        deploy_correlation_agent(),
        metric_anomaly_agent(),
    ]
