"""写工具 —— incident 管理、kubernetes 修复、升级。

副作用分级:
  LOW    —— incident 备注、状态更新（可逆的文本写）
  HIGH   —— pod 重启 / 回滚（不可逆，需要运维审批）
"""

from __future__ import annotations

import asyncio
import time

from prodagent import SideEffectLevel, ToolMeta, tool


@tool(
    meta=ToolMeta(
        name="open_incident",
        side_effect_level=SideEffectLevel.LOW,
        reversibility=0.9,
        estimated_latency_ms=300,
        domain="incident",
    )
)
async def open_incident(title: str, severity: str, affected_services: list[str]) -> dict:
    """在 PagerDuty / Jira 开一个新 incident 记录，返回 incident_id。

    severity 确认后，作为 triage 的第一个动作调用。

    Args:
        title:             简短 incident 标题
        severity:          P0 | P1 | P2 | P3
        affected_services: 涉及的服务名列表
    """
    incident_id = f"INC-{int(time.time()) % 100000000:08d}"
    return {
        "incident_id": incident_id,
        "title": title,
        "severity": severity,
        "affected_services": affected_services,
        "status": "open",
    }


@tool(
    meta=ToolMeta(
        name="update_incident",
        side_effect_level=SideEffectLevel.LOW,
        reversibility=0.9,
        estimated_latency_ms=300,
        domain="incident",
        resource_id="incident-tracker",
    )
)
async def update_incident(
    incident_id: str,
    status: str,
    message: str,
    next_steps: str = "",
) -> dict:
    """给已有 incident 记录（PagerDuty / Jira）加一条状态更新。

    [PURPOSE] 记录修复过程中的发现、状态变更和已执行动作。
    [DISTINCT] 与 restart_pod 或 scale_deployment（改基础设施）不同，
    这个只写文本 —— 不动 Kubernetes。不接受 service、pod_name 或任何
    基础设施参数。
    [PARAMS] incident_id 来自 open_incident；status 是四个固定值之一；
    message 是备注正文（其他系统里也叫 'summary' 或 'note' —— 这里
    用 'message'）。
    [CONSTRAINT] 先调 open_incident 拿到 incident_id。每次重大发现或完成
    动作后用。
    [EXAMPLE] update_incident(incident_id='INC-00001234', status='investigating',
    message='在 payment-service 上发现 OOMKilled pod，正在重启受影响 pod')

    Args:
        incident_id: open_incident 返回的 incident ID（如 'INC-00001234'）
        status:      investigating | mitigated | resolved | needs_code_fix
        message:     发现了什么或做了什么（更新文本 / 备注正文）。用 'message'，不是 'summary'、'note' 或 'intent'。
        next_steps:  agent run 结束后人工需要做什么（可选）
    """
    return {
        "incident_id": incident_id,
        "status": status,
        "updated": True,
        "next_steps_preview": next_steps[:200],
    }


@tool(
    meta=ToolMeta(
        name="restart_pod",
        side_effect_level=SideEffectLevel.HIGH,
        reversibility=0.1,
        estimated_latency_ms=3000,
        domain="kubernetes",
        resource_id="kubernetes-cluster",
    )
)
async def restart_pod(service: str, pod_name: str, reason: str = "") -> dict:
    """通过删除重启一个特定的 Kubernetes pod（幂等 —— 可安全重试）。

    [PURPOSE] 修复 OOMKilled、CrashLooping 或卡住需要回收的 pod。
    [DISTINCT] 与 update_incident（记文本备注）或 scale_deployment（改副本数）
    不同，这个针对单个 pod。不接受 incident_id 或任何 incident-tracker 参数。
    [PARAMS] 同时需要 service（deployment 名）和 pod_name（带随机 hash 后缀
    的完整 pod 名）。pod_name 从 get_pod_status 输出里拿。
    [CONSTRAINT] HIGH 副作用 —— 触发运维审批门禁。调这个之前始终先调
    get_pod_status 拿到带 hash 后缀的准确 pod_name。
    [EXAMPLE] restart_pod(service='payment-service',
    pod_name='payment-service-7d9f8b-mq9r', reason='OOMKilled 5 次')

    Args:
        service:  Deployment 名（如 'payment-service'）—— 父资源，不是 pod 名，不是 incident_id
        pod_name: get_pod_status 返回的带随机 hash 后缀的完整 pod 名（如 'payment-service-7d9f8b-mq9r'）—— 不是服务名
        reason:   重启简短原因（审计用）
    """
    await asyncio.sleep(0.05)  # simulate API call latency
    return {
        "pod_name": pod_name,
        "service": service,
        "status": "accepted",
        "reason": reason,
    }


@tool(
    meta=ToolMeta(
        name="rollback",
        side_effect_level=SideEffectLevel.HIGH,
        reversibility=0.1,
        estimated_latency_ms=5000,
        domain="kubernetes",
        resource_id="kubernetes-cluster",
    )
)
async def rollback(service: str, sha: str, reason: str = "") -> dict:
    """把 Kubernetes deployment 回滚到之前某个部署 SHA。

    [PURPOSE] get_pr_diff 确认可疑 SHA 是根因后，回退与部署相关的代码
    regression。
    [DISTINCT] 与 restart_pod（回收单个 pod）不同，这个回滚整个 deployment
    —— 是代码变更，不是 pod 回收。
    [CONSTRAINT] HIGH 副作用 —— 触发运维审批门禁。始终先调 get_pr_diff
    确认 SHA 是根因。没有开 incident 记录证据的情况下绝不回滚。

    Args:
        service: Deployment 名（如 'payment-service'）
        sha:     要回滚到的目标 SHA（get_recent_deploys 里的上一个好部署）
        reason:  简短原因（审计用）
    """
    await asyncio.sleep(0.05)
    return {
        "service": service,
        "rolled_back_to": sha,
        "status": "accepted",
        "reason": reason,
    }


@tool(
    meta=ToolMeta(
        name="scale_deployment",
        side_effect_level=SideEffectLevel.MEDIUM,
        reversibility=0.5,
        estimated_latency_ms=2000,
        domain="kubernetes",
    )
)
async def scale_deployment(service: str, replicas: int, reason: str = "") -> dict:
    """扩缩容一个 Kubernetes deployment。

    Args:
        service:  Deployment 名
        replicas: 目标副本数（1–20）
        reason:   简短原因（审计用）
    """
    replicas = max(1, min(replicas, 20))
    await asyncio.sleep(0.02)
    return {"service": service, "replicas": replicas, "status": "scaling"}


@tool(
    meta=ToolMeta(
        name="silence_alert",
        side_effect_level=SideEffectLevel.LOW,
        reversibility=0.9,
        estimated_latency_ms=500,
        domain="observability",
    )
)
async def silence_alert(alert_name: str, duration_minutes: int = 60, reason: str = "") -> dict:
    """在已知 incident 期间静默一个 PagerDuty/Alertmanager 告警。

    Args:
        alert_name:       要静默的告警名
        duration_minutes: 静默时长（最多 480 分钟 = 8 小时）
        reason:           incident ID 或静默原因
    """
    duration_minutes = min(duration_minutes, 480)
    return {
        "alert": alert_name,
        "silenced": True,
        "duration": duration_minutes,
        "reason": reason,
    }


@tool(
    meta=ToolMeta(
        name="create_incident_note",
        side_effect_level=SideEffectLevel.LOW,
        reversibility=0.9,
        estimated_latency_ms=300,
        domain="incident",
        resource_id="incident-tracker",
    )
)
async def create_incident_note(incident_id: str, note: str, author: str = "sentinel-agent") -> dict:
    """往 PagerDuty/Jira 的 incident 时间线发一条备注。

    Args:
        incident_id: incident 标识
        note:        备注文本（支持 Markdown）
        author:      作者署名
    """
    return {"incident_id": incident_id, "status": "posted", "author": author}
