"""Lock 1 —— FakeLLM 脚本（chapter 18 §01）。

在特定 LLM 输出下测试框架的编排逻辑 —— 不是测 LLM 的智力。FakeLLM
是系统骨架的 X 光机，不是欺骗工具。

``oom_happy_path_script()`` 驱动完整的 investigate→remediate OOM 故障流。
golden 场景构造失败路径: 测试围栏是否拦住越界，不是测 LLM 答得对不对。

investigator spawn 子 agent 的架构需要 routing LLM，因为子 agent 共享
父 LLM 但每个需要不同的响应。脚本返回一个 ``RoutingFakeLLM``，按
system prompt 分发到 per-agent 队列:
  · ``set_default([...])`` —— investigator（父）
  · ``llm.add(name, [...])`` —— log_analysis / deploy_correlation /
    metric_anomaly / remediator 各自的队列
"""

from __future__ import annotations

from prodagent import RoutingFakeLLM
from prodagent.kernel.types import LLMResponse, ToolCall


def oom_happy_path_script() -> RoutingFakeLLM:
    """新 investigator→child + peer remediator 架构下的 OOM 故障完整流。

    investigator 并行 fan-out 3 个诊断子 agent（log_analysis、
    deploy_correlation、metric_anomaly），合成一个 IncidentReport，然后
    **handoff_to_remediator** 把 report 交给 remediator peer —— 这结束
    investigator 的 run（COMPLETED），remediator 作为 peer continuation
    接过 report，跑 open_incident → update_incident → rollback →
    check_slo → update_incident（postmortem）。

    返回一个 ``RoutingFakeLLM``，按 system prompt 分发到 per-agent 队列，
    让并发子 agent 调用不会在共享队列上 race。remediator 作为 peer 有
    自己的 ``spec.llm``（``_build_peer_agent`` 复制），但 fake 模式下传的
    是同一个 RoutingFakeLLM —— 按 "# remediator Agent" system prompt 路由
    到 remediator 队列。
    """
    llm = RoutingFakeLLM()

    # ── Investigator（父）──
    # Turn 1: 一个 turn 里发 3 个 spawn_agent（并行 fan-out）。
    # Turn 2: 带合成的 IncidentReport handoff_to_remediator —— 结束 investigator。
    llm.set_default(
        [
            LLMResponse(
                content="并行 fan-out 诊断给 3 个专家。",
                tool_calls=[
                    ToolCall(
                        name="spawn_agent",
                        params={
                            "name": "log_analysis",
                            "task": "拉 payment-service 日志，找 OOM / 错误签名。",
                        },
                    ),
                    ToolCall(
                        name="spawn_agent",
                        params={
                            "name": "deploy_correlation",
                            "task": "查 payment-service 近期部署，看是否相关。",
                        },
                    ),
                    ToolCall(
                        name="spawn_agent",
                        params={
                            "name": "metric_anomaly",
                            "task": "查 payment-service 指标，算 SLO burn rate。",
                        },
                    ),
                ],
                stop_reason="tool_use",
            ),
            LLMResponse(
                content=(
                    "根因确认: PR #4412（commit a3f92b1）移除了 ProcessBatch() 里的 "
                    "buffer-pool 复用 —— 堆从 512 MiB → 3.8 GiB，OOMKill exit 137 "
                    "×5。带 IncidentReport handoff 给 remediator peer。"
                ),
                tool_calls=[
                    ToolCall(
                        name="handoff_to_remediator",
                        params={
                            "task": (
                                "修复 INC-OOM-001。IncidentReport: "
                                '{"severity":"P1","root_cause":"OOMKilled —— PR #4412 '
                                '移除了 buffer-pool 复用","suspicious_sha":"a3f92b1",'
                                '"rollback_target_sha":"f8c01d4","recommended_action":'
                                '"rollback","pod_name":"payment-service-7d9f8b-mq9r"}'
                            ),
                        },
                    )
                ],
                stop_reason="tool_use",
            ),
        ]
    )

    # ── log_analysis 子 ──
    # Turn 1: 调 tail_logs + get_pod_status。
    # Turn 2: 报告发现。
    llm.add(
        "log_analysis",
        [
            LLMResponse(
                content="拉日志 + 查 pod 状态。",
                tool_calls=[
                    ToolCall(
                        name="tail_logs",
                        params={"service": "payment-service", "lines": 100, "grep": "OOM"},
                    ),
                    ToolCall(name="get_pod_status", params={"service": "payment-service"}),
                ],
                stop_reason="tool_use",
            ),
            LLMResponse(
                content=(
                    "发现: 最近 10 分钟 OOMKilled（exit 137）×5。内存从 "
                    "512MiB→3.8GiB。pod payment-service-7d9f8b-mq9r 处于 "
                    "CrashLoopBackOff。日志签名: ProcessBatch() 里的堆分配"
                    "没有复用 buffer-pool。"
                ),
                stop_reason="end_turn",
            ),
        ],
    )

    # ── deploy_correlation 子 ──
    llm.add(
        "deploy_correlation",
        [
            LLMResponse(
                content="查近期部署和 PR diff。",
                tool_calls=[
                    ToolCall(
                        name="get_recent_deploys",
                        params={"service": "payment-service"},
                    ),
                    ToolCall(
                        name="get_pr_diff",
                        params={"service": "payment-service", "pr_id": "4412"},
                    ),
                ],
                stop_reason="tool_use",
            ),
            LLMResponse(
                content=(
                    "发现: 发生前 27 分钟部署了 v2.14.1（commit a3f92b1，"
                    "PR #4412）。PR diff 显示 ProcessBatch() 里移除了 "
                    "buffer-pool 复用。上一个好 SHA: f8c01d4（v2.14.0）。"
                    "相关。"
                ),
                stop_reason="end_turn",
            ),
        ],
    )

    # ── metric_anomaly 子 ──
    llm.add(
        "metric_anomaly",
        [
            LLMResponse(
                content="查指标算 SLO burn rate。",
                tool_calls=[
                    ToolCall(
                        name="query_metrics",
                        params={"service": "payment-service", "metric": "error_rate"},
                    ),
                ],
                stop_reason="tool_use",
            ),
            LLMResponse(
                content=(
                    "发现: SLO burn rate 14.2x。错误率 34%（5xx）。内存 "
                    "RSS 3.8GiB（基线 512MiB）。burn rate 由 OOMKill 级联"
                    "驱动 —— pod 重启比它能服务还快。"
                ),
                stop_reason="end_turn",
            ),
        ],
    )

    # ── remediator 子 ──（plan-and-resolve —— 产出 plan JSON）
    remediate_plan = (
        '{"steps": ['
        '{"id":"s1","action":"open_incident",'
        '"params":{"title":"payment-service OOM 级联 —— SLO burn 14.2x",'
        '"severity":"P1","affected_services":["payment-service","checkout-service"]},'
        '"depends_on":[]},'
        '{"id":"s2","action":"update_incident",'
        '"params":{"incident_id":"INC-20260619-001","status":"investigating",'
        '"message":"CrashLoopBackOff OOM 与部署 v2.14.1（commit '
        "a3f92b1, PR #4412）相关。根因: ProcessBatch() 移除了 buffer-pool 复用 —— "
        "堆从 512 MiB → 3.8 GiB，exit 137 ×5。回滚目标: 上一个好 SHA "
        'f8c01d4 (v2.14.0)。请求运维审批。","next_steps":"把 '
        "payment-service 回滚到 f8c01d4 (v2.14.0) —— 恢复 ProcessBatch() 里的 "
        'buffer-pool 复用。"},"depends_on":["s1"]},'
        '{"id":"s3","action":"rollback",'
        '"params":{"service":"payment-service","sha":"f8c01d4",'
        '"reason":"回退 PR #4412 的 buffer-pool regression，它导致了 OOMKill 级联"},'
        '"depends_on":["s2"]},'
        '{"id":"s4","action":"check_slo",'
        '"params":{"service":"payment-service"},"depends_on":["s3"]},'
        '{"id":"s5","action":"update_incident",'
        '"params":{"incident_id":"INC-20260619-001","status":"mitigated",'
        '"message":"payment-service 已回滚到 f8c01d4 (v2.14.0)。SLO burn rate '
        '正在恢复。pod 正在回收 —— buffer-pool 复用已恢复。","next_steps":'
        '"观察 SLO 30 分钟。加 buffer-pool 回归测试。在 80% 内存阈值加 OOM '
        '告警。"},"depends_on":["s4"]}'
        "]}"
    )
    llm.add(
        "remediator",
        [
            LLMResponse(content=remediate_plan, stop_reason="end_turn"),
            LLMResponse(
                content=(
                    "## Incident INC-20260619-001 —— 已回滚到 v2.14.0\n\n"
                    "**根因:** PR #4412（commit a3f92b1）移除了 `ProcessBatch()` "
                    "里的 buffer-pool 复用 —— per-item 分配把堆从 512 MiB → "
                    "3.8 GiB，OOMKill exit 137 ×5，CrashLoopBackOff。\n\n"
                    "**时间线:**\n"
                    "  01:47Z —— 部署 v2.14.1（a3f92b1）\n"
                    "  02:13Z —— OOMKill 级联（×5 重启，exit 137）\n"
                    "  02:17Z —— 运维批准回滚到 f8c01d4（v2.14.0）\n"
                    "  02:18Z —— SLO burn rate 恢复中，pod 回收中\n\n"
                    "**修复:** payment-service 从 a3f92b1 回滚到 f8c01d4，恢复"
                    "`ProcessBatch()` 里的 buffer-pool 复用。pod 回收中；"
                    "SLO burn rate 趋向 1x。"
                ),
                stop_reason="end_turn",
            ),
        ],
    )

    return llm
