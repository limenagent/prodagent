"""L3 升级工具 —— MEDIUM 副作用，无需审批即可执行。

审批门只拦截 HIGH 工具，MEDIUM 直接执行。对照 HIGH 工具
（restart_pod、rollback），那些必须暂停等运维签字。
"""

from __future__ import annotations

from prodagent import SideEffectLevel, ToolMeta, tool


@tool(
    meta=ToolMeta(
        name="page_oncall",
        side_effect_level=SideEffectLevel.MEDIUM,
        timeout_seconds=1000 / 1_000,
        domain="incident",
    )
)
async def page_oncall(team: str, message: str, severity: str = "P2") -> dict:
    """通过 PagerDuty 升级给人工 oncall。

    MEDIUM 副作用 —— 无需运维审批即可执行。根因不清或 SLO burn 高时
    随时用。

    Args:
        team:     oncall 团队名（如 platform、payment、database）
        message:  PagerDuty 里显示的升级消息
        severity: incident 严重度（P0|P1|P2|P3）
    """
    return {"team": team, "paged": True, "severity": severity}
