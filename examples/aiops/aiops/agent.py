"""AIOps 故障应急 Agent —— 组装示例。"""

from __future__ import annotations

import os
from pathlib import Path

from prodagent import Agent
from prodagent.core.config import FrameworkConfig
from prodagent.evaluation.skills.registry import SkillRegistry
from prodagent.llm.base import LLMClient

from aiops.child_agents import (
    diagnostic_child_agents,
    remediator_agent,
)
from aiops.testing.fake_llm_scripts import oom_happy_path_script
from aiops.tools import page_oncall
from aiops.tools.registry import build_aiops_tool_registry

_BASE = Path(__file__).parent
SKILLS_DIR = _BASE / "skills"

_SYSTEM_PROMPT = """\
你是 AIOps 故障应急 agent。运维人员会描述一个生产故障。先调查，开 \
incident，再安全地修复。

## 规则
- 任何修复前，必须先 open_incident

## 工作流
1. 把调查 fan-out 给三个只读专家子 agent —— 在同一个 turn 内发出全部\
三个 spawn_agent 调用（不要等一个再调下一个）:
  spawn_agent(name='log_analysis', task=...)
  spawn_agent(name='deploy_correlation', task=...)
  spawn_agent(name='metric_anomaly', task=...)
给每个子 agent 一个围绕本次 incident 的任务。

2. 把三方的发现合成成 IncidentReport。schema:
  {"reasoning": "<综合三方发现的思考链>",
   "severity": "P0|P1|P2|P3",
   "slo_burn_rate": <数字，未知用 0.0>,
   "root_cause": "<假设，或数据不足时为 Unknown>",
   "pod_name": "<名字或 N/A>",
   "suspicious_sha": "<如果与部署相关，可疑的 SHA；否则 N/A>",
   "rollback_target_sha": "<如果与部署相关，要回滚到的上一个好 SHA；否则 N/A>",
   "recommended_action": "restart_pod|rollback|scale|escalate|monitor"}

3. 根据 IncidentReport 路由:
   - 如果 root_cause 明确且 recommended_action 是 rollback 或 \
restart_pod: **handoff_to_remediator**(task=<把 IncidentReport \
JSON 传过去>) —— 这会结束你（investigator）的 run，remediator 作为 \
peer 接过 report 继续：open_incident、执行修复、验证、写 postmortem。
   - 如果 root_cause 是 'Unknown' 或 recommended_action 是 escalate: \
直接调 page_oncall —— 不要 handoff 给 remediator。

相关时用 get_skill(name=...) 加载领域 runbook。

"""


DEFAULT_TASK = "支付服务有告警。"


def build_aiops_agent(
    *,
    llm: LLMClient | None = None,
    framework_config: FrameworkConfig | None = None
) -> Agent:
    """fake 模式用脚本化的 RoutingFakeLLM（确定的 OOM 故障轨迹）；
    否则不传 llm —— 框架从 env lazy resolve 真 LLM。evals 显式传 llm。
    """
    use_fake = os.getenv("USE_FAKE_LLM", "").lower() in ("1", "true", "yes")
    resolved_llm = llm or (oom_happy_path_script() if use_fake else None)
    return (
        Agent(
            "investigate",
            tools=[page_oncall],
            tool_registry=build_aiops_tool_registry(),
            skills=SkillRegistry.from_dir(SKILLS_DIR),
            context=_SYSTEM_PROMPT,
            llm=resolved_llm,
            framework_config=framework_config,
        )
        .agents(diagnostic_child_agents())
        .peers([remediator_agent(llm=resolved_llm)])
        .reactive()
        .budget(turns=20, cost_usd=1.0, seconds=1800.0)
    )
