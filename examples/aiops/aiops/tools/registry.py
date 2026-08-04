"""工具注册表 —— 分层 × 角色限定的工具矩阵。"""

from __future__ import annotations

from prodagent.tooling.registry import ToolRegistry

from aiops.tools import (
    capture_dashboard,
    check_slo,
    create_incident_note,
    get_pod_status,
    get_pr_diff,
    get_recent_deploys,
    open_incident,
    page_oncall,
    query_metrics,
    restart_pod,
    rollback,
    scale_deployment,
    silence_alert,
    tail_logs,
    update_incident,
)


def build_aiops_tool_registry() -> ToolRegistry:
    """组装 agent 用的分层 × 角色限定工具矩阵。"""
    registry = ToolRegistry()

    # l1: 永远安全的底线 —— 只读探针，每个阶段都可用。
    for t in [query_metrics, check_slo]:
        registry.register(t, tier="l1")

    # l2 / investigate: 完整只读诊断工具集。
    for t in [
        query_metrics,
        check_slo,
        tail_logs,
        get_pod_status,
        get_recent_deploys,
        get_pr_diff,
        capture_dashboard,
    ]:
        registry.register(t, tier="l2", role="investigate")

    # l2 / remediate: incident 管理 + 基础设施动作。包含 check_slo，
    # 因为规则要求任何 restart/rollback 后都要有一次恢复观测。
    for t in [
        check_slo,
        open_incident,
        update_incident,
        create_incident_note,
        restart_pod,
        rollback,
        scale_deployment,
        silence_alert,
        page_oncall,
    ]:
        registry.register(t, tier="l2", role="remediate")
    return registry
