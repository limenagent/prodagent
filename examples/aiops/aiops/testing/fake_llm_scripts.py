"""Lock 1 —— FakeLLM 脚本（chapter 18 §01）。

在特定 LLM 输出下测试框架的编排逻辑 —— 不是测 LLM 的智力。FakeLLM
是系统骨架的 X 光机，不是欺骗工具。

脚本:
  · oom_happy_path_script()       —— 完整 investigate→remediate OOM 故障流
  · hallucination_fence_script()  —— LLM 幻觉一个不存在的工具；框架必须拦截
  · crashloop_script()            —— CrashLoopBackOff 无 OOM 证据 → 升级
  · bad_deploy_script()           —— 坏配置部署 → 回滚路径
  · metric_anomaly_script()       —— 纯指标异常，无日志证据 → 升级

golden 场景构造失败路径: 测试围栏是否拦住越界，不是测 LLM 答得对不对。

新架构（investigator spawn 子 agent）需要 routing LLM，因为子 agent 共享
父 LLM 但每个需要不同的响应。``oom_happy_path_script()`` 返回一个
``RoutingFakeLLM``，按 system prompt 分发到 per-agent 队列。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from prodagent import FakeLLMAdapter, LLMClient, LLMConfig
from prodagent.core.types import LLMResponse, MessageList, ToolCall

from aiops.testing.routing_fake_llm import RoutingFakeLLM

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


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

    # ── remediator 子 ──（PLAN_FIRST —— 产出 plan JSON）
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


def hallucination_fence_script() -> FakeLLMAdapter:
    """围栏测试: LLM 幻觉一个不存在的工具；框架必须拦截。

    Turn 1 返回 ``delete_table`` —— 不在工具注册表里。框架必须拒绝执行
    并把错误反馈给 LLM。Turn 2 改成合法的 ``check_slo``。

    测的是"LLM 错的时候框架能不能拦住"，不是"LLM 能不能答对"。
    """
    return FakeLLMAdapter(
        responses=[
            # Turn 1 —— 幻觉: 调不存在的工具
            LLMResponse(
                content="先清掉 users 表再说。",
                tool_calls=[ToolCall(name="delete_table", params={"table": "users"})],
                stop_reason="tool_use",
            ),
            # Turn 2 —— 纠正: 合法只读诊断
            LLMResponse(
                content="工具不存在，改为查 SLO。",
                tool_calls=[ToolCall(name="check_slo", params={"service": "payment-service"})],
                stop_reason="tool_use",
            ),
            # Turn 3 —— 结构化输出（满足 investigate 阶段 output_schema）
            LLMResponse(
                content=(
                    '{"reasoning": "幻觉工具被拒；回退到 check_slo。", '
                    '"severity": "P2", '
                    '"slo_burn_rate": 1.0, '
                    '"root_cause": "Unknown —— 数据不足", '
                    '"pod_name": "N/A", '
                    '"recommended_action": "monitor"}'
                ),
                stop_reason="end_turn",
            ),
        ]
    )


def crashloop_script() -> FakeLLMAdapter:
    """CrashLoopBackOff 路径（无 OOM 证据 → 升级）。

    pod 反复重启但日志没有 OOMKilled，指标没有内存尖峰。agent 不能盲目
    restart（会继续 CrashLoop）或 scale（可能放大节点级问题）—— 正确动作
    是升级给 oncall。
    """
    remediate_plan = (
        '{"steps": ['
        '{"id":"s1","action":"open_incident",'
        '"params":{"title":"checkout-api CrashLoopBackOff —— 根因不明",'
        '"severity":"P2","affected_services":["checkout-api"]},'
        '"depends_on":[]},'
        '{"id":"s2","action":"update_incident",'
        '"params":{"incident_id":"INC-20260619-002","status":"needs_oncall",'
        '"message":"pod checkout-api-5c8d2a-x7f3 处于 CrashLoopBackOff（4 次重启，exit 1）。 '
        "日志无 OOMKilled，内存稳定在 200MiB。近 24h 无部署。"
        '从遥测无法确定根因 —— 需要 oncall 检查节点状态。",'
        '"next_steps":"1. page checkout on-call。\\n'
        "2. 检查节点健康（磁盘压力、kubelet）。\\n"
        '3. 用 kubectl logs --previous 看上一个 pod 的日志。"},'
        '"depends_on":["s1"]}'
        "]}"
    )

    return FakeLLMAdapter(
        responses=[
            # Turn 1 —— 初始诊断
            LLMResponse(
                content="checkout-api 在 CrashLoopBackOff，先查 SLO 和 pod 状态。",
                tool_calls=[
                    ToolCall(name="check_slo", params={"service": "checkout-api"}),
                    ToolCall(name="get_pod_status", params={"service": "checkout-api"}),
                    ToolCall(
                        name="tail_logs",
                        params={"service": "checkout-api", "lines": 50, "grep": "error"},
                    ),
                ],
                stop_reason="tool_use",
            ),
            # Turn 2 —— 查近期部署（结果: 24h 无部署，排除坏部署）
            LLMResponse(
                content="日志显示 exit 1 但无 OOMKilled，内存稳定。查最近部署确认是否代码变更。",
                tool_calls=[
                    ToolCall(name="get_recent_deploys", params={"service": "checkout-api"}),
                    ToolCall(
                        name="query_metrics",
                        params={"service": "checkout-api", "metric": "memory_rss"},
                    ),
                ],
                stop_reason="tool_use",
            ),
            # Turn 3 —— 结构化总结
            LLMResponse(
                content=(
                    "SEVERITY: P2\n"
                    "SLO_BURN: 3.1x\n"
                    "ROOT_CAUSE: unknown —— CrashLoopBackOff，exit 1，无 OOM，无近期部署\n"
                    "POD_NAME: checkout-api-5c8d2a-x7f3\n"
                    "RECOMMENDED_ACTION: escalate\n"
                    "证据: 8 分钟内 4 次重启，每次 exit 1。内存稳定 200MiB。"
                    "上次部署 26h 前。需要节点级排查。"
                ),
                stop_reason="end_turn",
            ),
            # Turn 4 —— 结构化 schema
            LLMResponse(
                content=(
                    '{"reasoning": "CrashLoopBackOff，exit 1（不是 OOM）。无部署相关。'
                    '内存稳定。从 app 级遥测无法确定根因。", '
                    '"severity": "P2", '
                    '"slo_burn_rate": 3.1, '
                    '"root_cause": "unknown —— exit 1 CrashLoopBackOff，无 OOM，无部署相关", '
                    '"pod_name": "checkout-api-5c8d2a-x7f3", '
                    '"recommended_action": "escalate"}'
                ),
                stop_reason="end_turn",
            ),
            # Turn 5 —— 修复 plan（open + update，无高风险工具）
            LLMResponse(content=remediate_plan, stop_reason="end_turn"),
            # Turn 6 —— postmortem 总结
            LLMResponse(
                content=(
                    "## Incident INC-20260619-002 —— 已升级给 on-call（根因不明）\n\n"
                    "**根因:** 从 app 遥测无法确定 —— pod exit 1 CrashLoopBackOff，"
                    "无 OOM，无部署相关，内存稳定。可能是节点级问题。\n\n"
                    "**为什么不自动修复:** restart 只会继续 CrashLoop；scale 可能"
                    "放大节点级问题；没有代码变更可以回滚。正确动作是升级，"
                    "让 oncall 检查节点。"
                ),
                stop_reason="end_turn",
            ),
        ]
    )


def bad_deploy_script() -> FakeLLMAdapter:
    """坏配置部署路径（明确的坏部署 → 回滚路径）。

    根因是配置变更（feature flag 翻转）。回滚是允许的 —— 有明确的 PR diff
    证据 + 运维审批。跑 open_incident → update_incident（记录根因 + 回滚
    目标）→ rollback（HIGH，运维审批）→ check_slo → update_incident。
    """
    remediate_plan = (
        '{"steps": ['
        '{"id":"s1","action":"open_incident",'
        '"params":{"title":"payment-service 配置 regression —— 坏 feature flag",'
        '"severity":"P1","affected_services":["payment-service"]},'
        '"depends_on":[]},'
        '{"id":"s2","action":"update_incident",'
        '"params":{"incident_id":"INC-20260619-003","status":"investigating",'
        '"message":"PR #4501 把 feature flag `new_checkout_flow` 从 false→true '
        "改到生产配置，没有 canary。错误率在部署后 5 分钟内从 8% → 22%。"
        '回滚目标: 上一个好 SHA 2a1b8e0 (v2.10.4)。请求运维审批。",'
        '"next_steps":"把 payment-service 回滚到 2a1b8e0 —— 恢复 new_checkout_flow=false。"},'
        '"depends_on":["s1"]},'
        '{"id":"s3","action":"rollback",'
        '"params":{"service":"payment-service","sha":"2a1b8e0",'
        '"reason":"回退 PR #4501 配置 regression —— new_checkout_flow flag 翻转"},'
        '"depends_on":["s2"]},'
        '{"id":"s4","action":"check_slo",'
        '"params":{"service":"payment-service"},'
        '"depends_on":["s3"]},'
        '{"id":"s5","action":"update_incident",'
        '"params":{"incident_id":"INC-20260619-003","status":"mitigated",'
        '"message":"payment-service 已回滚到 2a1b8e0。错误率从 22%→2% 恢复中。'
        'new_checkout_flow=false 已恢复。",'
        '"next_steps":"给配置部署流水线加 canary 阶段。复盘 flag 翻转审批流程。"},'
        '"depends_on":["s4"]}'
        "]}"
    )

    return FakeLLMAdapter(
        responses=[
            # Turn 1 —— 诊断
            LLMResponse(
                content="payment-service 错误率突增，查 SLO + 最近部署。",
                tool_calls=[
                    ToolCall(name="check_slo", params={"service": "payment-service"}),
                    ToolCall(
                        name="query_metrics",
                        params={"service": "payment-service", "metric": "error_rate"},
                    ),
                    ToolCall(name="get_recent_deploys", params={"service": "payment-service"}),
                ],
                stop_reason="tool_use",
            ),
            # Turn 2 —— 拉 PR diff（明确证据）
            LLMResponse(
                content="发现 8 分钟前部署 PR #4501，查 PR diff 确认变更内容。",
                tool_calls=[
                    ToolCall(
                        name="get_pr_diff", params={"service": "payment-service", "pr_id": "4501"}
                    ),
                    ToolCall(
                        name="tail_logs",
                        params={"service": "payment-service", "lines": 50, "grep": "error"},
                    ),
                ],
                stop_reason="tool_use",
            ),
            # Turn 3 —— 结构化总结
            LLMResponse(
                content=(
                    "SEVERITY: P1\n"
                    "SLO_BURN: 6.8x\n"
                    "ROOT_CAUSE: PR #4501 在生产开了 new_checkout_flow 且没有 canary —— 错误率 8%→22%\n"
                    "SUSPICIOUS_SHA: 7e2c9d4\n"
                    "ROLLBACK_TARGET: 2a1b8e0（v2.10.4，上一个好部署）\n"
                    "RECOMMENDED_ACTION: rollback\n"
                    "证据: PR diff 显示 flag 翻转，错误率与部署时间相关。"
                ),
                stop_reason="end_turn",
            ),
            # Turn 4 —— schema
            LLMResponse(
                content=(
                    '{"reasoning": "配置 regression。PR #4501 在生产把 new_checkout_flow=true。'
                    '错误率 5 分钟内从 8%→22%。回滚到上一个好 SHA 2a1b8e0 —— 需要运维审批。", '
                    '"severity": "P1", '
                    '"slo_burn_rate": 6.8, '
                    '"root_cause": "PR #4501（commit 7e2c9d4）在生产开了 new_checkout_flow 且没有 canary", '
                    '"suspicious_sha": "7e2c9d4", '
                    '"recommended_action": "rollback"}'
                ),
                stop_reason="end_turn",
            ),
            # Turn 5 —— plan: open → update → rollback → check_slo → update
            LLMResponse(content=remediate_plan, stop_reason="end_turn"),
            # Turn 6 —— postmortem
            LLMResponse(
                content=(
                    "## Incident INC-20260619-003 —— 已回滚到 v2.10.4\n\n"
                    "**根因:** PR #4501（commit 7e2c9d4）在生产配置里开了 "
                    "new_checkout_flow，没有 canary 阶段。错误率 5 分钟内从 "
                    "8% → 22%。\n\n"
                    "**修复:** 经运维审批，payment-service 从 7e2c9d4 回滚到 "
                    "2a1b8e0（v2.10.4）—— 恢复 new_checkout_flow=false。错误率"
                    "恢复中。\n\n"
                    "**后续跟进:**\n"
                    "  1. 给配置部署流水线加 canary 阶段\n"
                    "  2. 复盘 flag 翻转审批流程 —— 应该要求双人签字\n"
                    "  3. 在 staging 加 flag 状态回归测试"
                ),
                stop_reason="end_turn",
            ),
        ]
    )


def metric_anomaly_script() -> FakeLLMAdapter:
    """纯指标异常路径（无日志证据 → 升级）。

    指标异常但日志干净。agent 不能猜根因 —— 升级。禁止: restart_pod /
    scale_deployment（无证据的盲目变更）。
    """
    remediate_plan = (
        '{"steps": ['
        '{"id":"s1","action":"open_incident",'
        '"params":{"title":"order-service 延迟异常 —— 无日志证据",'
        '"severity":"P2","affected_services":["order-service"]},'
        '"depends_on":[]},'
        '{"id":"s2","action":"update_incident",'
        '"params":{"incident_id":"INC-20260619-004","status":"needs_oncall",'
        '"message":"order-service p99 延迟 800ms→2400ms。日志干净（无错误，无 OOM）。'
        '12h 无部署。指标异常但无日志关联 —— 需要 oncall 检查下游依赖。",'
        '"next_steps":"1. page order on-call。\\n'
        "2. 检查下游服务（DB、cache、payment-api）。\\n"
        '3. 看慢查询日志。"},'
        '"depends_on":["s1"]}'
        "]}"
    )

    return FakeLLMAdapter(
        responses=[
            # Turn 1 —— 查指标 + 日志
            LLMResponse(
                content="order-service p99 延迟突增，查 metric 和 logs。",
                tool_calls=[
                    ToolCall(name="check_slo", params={"service": "order-service"}),
                    ToolCall(
                        name="query_metrics",
                        params={"service": "order-service", "metric": "p99_latency"},
                    ),
                    ToolCall(
                        name="tail_logs",
                        params={"service": "order-service", "lines": 50, "grep": "error"},
                    ),
                ],
                stop_reason="tool_use",
            ),
            # Turn 2 —— 查近期部署（排除部署因素）
            LLMResponse(
                content="p99 800ms→2400ms 但 logs 干净。查最近部署确认。",
                tool_calls=[
                    ToolCall(name="get_recent_deploys", params={"service": "order-service"}),
                    ToolCall(name="get_pod_status", params={"service": "order-service"}),
                ],
                stop_reason="tool_use",
            ),
            # Turn 3 —— 结构化总结
            LLMResponse(
                content=(
                    "SEVERITY: P2\n"
                    "SLO_BURN: 2.4x\n"
                    "ROOT_CAUSE: unknown —— 延迟尖峰，无日志/部署关联\n"
                    "RECOMMENDED_ACTION: escalate\n"
                    "证据: p99 800ms→2400ms，日志干净，12h 无部署。"
                    "可能是下游依赖问题，但无法从 app 遥测确认。"
                ),
                stop_reason="end_turn",
            ),
            # Turn 4 —— schema
            LLMResponse(
                content=(
                    '{"reasoning": "延迟异常无日志关联。12h 无部署。'
                    '无法从 app 级遥测确认根因。", '
                    '"severity": "P2", '
                    '"slo_burn_rate": 2.4, '
                    '"root_cause": "unknown —— p99 延迟尖峰，日志干净，无部署关联", '
                    '"recommended_action": "escalate"}'
                ),
                stop_reason="end_turn",
            ),
            # Turn 5 —— plan
            LLMResponse(content=remediate_plan, stop_reason="end_turn"),
            # Turn 6 —— postmortem
            LLMResponse(
                content=(
                    "## Incident INC-20260619-004 —— 延迟异常已升级\n\n"
                    "**根因:** 从 app 遥测无法确定 —— p99 延迟 800ms→2400ms，"
                    "日志干净，无部署关联。可能是下游依赖问题。\n\n"
                    "**为什么不自动修复:** 没有证据指向任何具体动作；"
                    "没有假设就 restart 或 scale 是盲目的。正确动作是升级。"
                ),
                stop_reason="end_turn",
            ),
        ]
    )


class _AuxFakeLLM(LLMClient):
    """fake 模式下驱动记忆分类 + 技能合成的离线 aux LLM。

    按 system prompt 路由，让两个 SESSION_END 消费者（MemoryClassifier
    和 SkillSynthesizer）从独立响应取值，即使事件 handler 并发派发:

    * "memory extraction" prompt → ``{"memory_type":"none"}``，让分类器
      返回 None（跳过，不写垃圾 EPISODIC）。
    * "process analyst" prompt → 空 content，让合成器返回失败结果，
      LearningHooks 打印它的 "skipped" 行。
    """

    _NONE_JSON = (
        '{"memory_type":"none","content":"","domain":"general","ttl_days":null,"entity_id":""}'
    )

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: Callable[[str], Awaitable[None]],
    ) -> LLMResponse:
        if "memory extraction" in system:
            return LLMResponse(
                content=self._NONE_JSON,
                stop_reason="end_turn",
                input_tokens=50,
                output_tokens=10,
            )
        # 合成器（或其他）: 空 content → SynthesisResult(failure=...)。
        return LLMResponse(content="", stop_reason="end_turn", input_tokens=50, output_tokens=0)


def aux_fake_llm() -> LLMClient:
    """返回记忆分类 + 技能合成用的离线 aux LLM。"""
    return _AuxFakeLLM()
