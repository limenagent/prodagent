"""邮件分拣 Agent —— REACTIVE 主 agent + workflow 子 agent + HITL 分级审批。

本示例展示:
  - **主 agent REACTIVE** —— ``email_triage`` 主 agent 是对话入口,
    REACTIVE 模式。用户说"分拣收件箱" → 主 agent 调 ``spawn_agent``
    委派给 ``triage_workflow`` 子 agent 跑固定 DAG → 拿到汇总后继续
    对话(追问某封邮件、要求重分、讨论分类结果)。DAG 跑完不阻塞
    对话 —— 主 agent 永远可交互。
  - **workflow 子 agent** —— ``triage_workflow`` 是 ``.workflow()`` 构造的
    固定 DAG(read_inbox → 4×classify ‖ → 4×route → summarize),通过
    ``spawn_agent`` 触发。DAG 跑完返回汇总给主 agent。子 agent 是固定
    流程,主 agent 是灵活对话 —— 两者职责分离。
  - **HITL 分级审批** —— ``SideEffectLevel`` 三级驱动 ``ApprovalHooks``:
    LOW(只读,不走门禁)、MEDIUM(可逆,自动批准+审计)、HIGH(不可逆 /
    外部爆炸半径 → 人工审批)。workflow 不绕过分级审批 ——
    ``delete_email`` HIGH 仍走门禁,子 agent 挂起时通过 ``spawn_agent``
    返回的 ``approval_request_id`` 传到父 run。

为什么主 agent REACTIVE + workflow 子 agent: 邮件分拣是对话场景 ——
用户会追问"eml_003 为什么删了"、"换一批邮件重分"、"刚才那封 newsletter
再确认一下"。固定 DAG 跑完就死,不能对话;REACTIVE 主 agent 永远在,
按需触发 DAG。
"""

from __future__ import annotations
import os
from pathlib import Path

from prodagent import Agent
from prodagent.core.config import FrameworkConfig
from prodagent.guardrail.approval.gate import ApprovalGate
from prodagent.hooks.bundles.security import ApprovalHooks
from prodagent.llm.base import LLMClient

from email_triage.fake_llm import build_fake_llm
from email_triage.tools import (
    archive_email,
    delete_email,
    forward_external,
    mark_read,
    read_inbox,
)
from email_triage.workflow import build_triage_workflow

_BASE = Path(__file__).parent
SKILLS_DIR = _BASE / "skills"

_MAIN_SYSTEM = """\
你是邮件分拣编排 agent。用户想分拣收件箱时,调 \
``spawn_agent(name="triage_workflow", task=...)`` 委派给固定的分拣 \
workflow(read_inbox → 4×classify → 4×route → summarize)。workflow 跑完 \
会返回分拣汇总,你把结果讲给用户。

## 规则
- 用户说"分拣"/"查邮件"/"清理收件箱"时,调 spawn_agent 触发 DAG。
  不要自己逐封分类 —— 那是 workflow 的事。
- DAG 跑完,把汇总(归档了哪些、标记了哪些、删了哪些、为什么)讲给用户。
- 用户追问某封邮件/要求重分/讨论分类结果时,直接对话 —— 不需要再跑 DAG。
- 用户要重新分拣(新一批邮件)时,再调一次 spawn_agent。
"""

_WORKFLOW_SYSTEM = """\
你是邮件分拣 workflow agent。DAG 写死: \
read_inbox_step → 4×classify ‖ → 4×route → summarize。你不需要生成 plan \
—— 直接执行。classify 和 summarize 是真 LLM 调用,分类决策和汇总都花 \
tokens。delete_email 是 HIGH 副作用,走 HITL 门禁。
"""


def build_triage_workflow_agent(
    *,
    llm: LLMClient | None = None,
    framework_config: FrameworkConfig | None = None,
    approval_gate: ApprovalGate | None = None,
) -> Agent:
    """triage_workflow 子 agent —— 固定 DAG(read_inbox → classify ‖ → route → summarize)。

    ``.workflow()`` 构造,DAG 写死跳过 LLM planning。classify/summarize 是
    真 LLM 标注,route 用 tool_step 引用已注册工具(HIGH delete_email 走门禁)。
    通过主 agent 的 ``spawn_agent`` 触发。

    Args:
        framework_config: 父 fw;不传时用 default。playground 注入带独立 namespace 的 fw。
        approval_gate: 共享的 ApprovalGate。子 agent 的 HIGH 工具(delete_email)
            挂起时,request_id 落到这个 gate 上 —— 父 run ``submit_approval``
            通过同一个 gate 解锁。不传时子 agent 自建一个独立 gate(只能
            子 agent 自己 submit,父 run 摸不到 —— 演示场景需要父 run 代审批)。
    """
    builder = (
        Agent(
            "triage_workflow",
            system_prompt=_WORKFLOW_SYSTEM,
            tools=[read_inbox, archive_email, mark_read, delete_email, forward_external],
            llm=llm,
            framework_config=framework_config,
        )
        .workflow(build_triage_workflow(), allow_replan=False)
        .budget(turns=15, cost_usd=0.40, seconds=120.0)
    )
    if approval_gate is not None:
        builder = builder.extend(ApprovalHooks(gate=approval_gate))
    return builder


DEFAULT_TASK = "分拣收件箱: 归档 newsletter,标记 action-needed 为已读,删除 phishing。"


def build_email_triage_agent(
    *,
    llm: LLMClient | None = None,
    framework_config: FrameworkConfig | None = None,
    approval_gate: ApprovalGate | None = None,
) -> Agent:
    """主 agent —— REACTIVE 对话入口。

    用户说"分拣收件箱" → 主 agent 调 ``spawn_agent(name="triage_workflow")``
    委派固定 DAG → 拿到汇总后继续对话。主 agent 永远可交互,DAG 跑完
    不阻塞对话。

    Args:
        llm: 主 agent 用的 LLM。不传时按 USE_FAKE_LLM 决定。
        framework_config: 父 fw;不传时用 default。playground 注入带独立 namespace 的 fw。
        approval_gate: 父子共享的 ApprovalGate。子 workflow 的 HIGH
            ``delete_email`` 挂起时,request_id 落到这个 gate;主 agent
            ``submit_approval`` 通过同一个 gate 放行。不传时自建一个,
            然后传给子 workflow —— 演示场景必需,否则父 run 摸不到子 agent
            的挂起请求。
    """
    use_fake = os.getenv("USE_FAKE_LLM", "").lower() in ("1", "true", "yes")
    resolved_llm = llm or (build_fake_llm() if use_fake else None)
    shared_gate = approval_gate or ApprovalGate()
    triage_workflow = build_triage_workflow_agent(
        llm=resolved_llm,
        framework_config=framework_config,
        approval_gate=shared_gate,
    )

    return (
        Agent(
            "email_triage",
            system_prompt=_MAIN_SYSTEM,
            tools=[read_inbox, archive_email, mark_read],
            llm=resolved_llm,
            framework_config=framework_config,
        )
        .reactive()
        .agents([triage_workflow])
        .extend(ApprovalHooks(gate=shared_gate))
        .budget(turns=20, cost_usd=0.50, seconds=180.0)
    )