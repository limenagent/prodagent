"""邮件分拣 workflow —— 把固定 DAG 写成 Python 代码,关键决策点用 wf.llm_step。

收件箱里 4 封邮件（eml_001~eml_004），每封走 classify → route 两步链。
4 个 classify 并行（都依赖 read_inbox_step），4 个 route 各自依赖自己的
classify，最后 summarize 汇总。

**classify 和 summarize 用 ``wf.llm_step``** —— 框架级 LLM 调用原语，
prompt 里的 ``{{upstream.output}}`` 模板由 ``Plan.resolve_params`` 在运行时
解析。不在工具内部手写 ``llm.complete()`` —— LLM 调用是框架驱动的。

route 步用 ``wf.tool_step`` 直接以已注册工具的名字（archive_email /
mark_read / delete_email）作为 step action —— 这样 PlanExecutor 通过
``ToolDispatcher`` 调它们，``delete_email`` 的 HIGH side-effect 照样触发
HITL 审批门禁。workflow 不绕过分级审批。
"""

from __future__ import annotations

from prodagent.core.types import ToolResult
from prodagent.runtime.workflow import Workflow

from email_triage.tools import read_inbox

_EMAIL_IDS = ["eml_001", "eml_002", "eml_003", "eml_004"]

# classify 的 suggested_action 映射到 route 工具名。archive / mark_read 是
# MEDIUM（自动批准），delete 是 HIGH（HITL 门禁）。LLM system prompt 里的
# 分类规则保证了 suggested_action 跟这张表一致。
_ROUTE_TABLE: dict[str, str] = {
    "eml_001": "archive_email",   # newsletter
    "eml_002": "mark_read",       # action_needed
    "eml_003": "delete_email",    # phishing → HIGH
    "eml_004": "archive_email",   # notification
}

_CLASSIFY_SYSTEM = """\
你是邮件分类器。读邮件元数据 + 正文，输出 JSON:
{"category": "newsletter|notification|action_needed|phishing|other",
 "suggested_action": "archive_email|mark_read|delete_email|keep"}

规则:
- 钓鱼（要求点击验证链接、可疑发件人）→ phishing / delete_email
- newsletter / digest → newsletter / archive_email
- PR 合并 / 系统通知 → notification / archive_email
- 需要人工跟进（老板要你 review） → action_needed / mark_read
- 其他 → other / keep
只输出 JSON，不要多余文字。"""


_SUMMARIZE_SYSTEM = """\
你是邮件分拣汇总器。读归档/标记/删除日志，输出一句中文总结。"""


def _value(result: ToolResult) -> dict:
    """从 FunctionTool 返回的 ToolResult 里拿原始 dict。"""
    return result.value if isinstance(result.value, dict) else {"result": result.value}


# classify 的 prompt 模板 —— {{read_inbox_step.output}} 是 Plan.resolve_params
# 模板，运行时解析成上游 step 的输出（dict 的 str repr）。email_id 在编译时
# 已知，直接 f-string 嵌入。
def _classify_prompt(email_id: str) -> str:
    return (
        f"邮件 ID: {email_id}\n"
        "收件箱内容: {{read_inbox_step.output}}\n"
        "请分类这封邮件（只输出 JSON）。"
    )


_SUMMARIZE_PROMPT = """\
分拣结果:
- eml_001: {{route_eml_001.output}}
- eml_002: {{route_eml_002.output}}
- eml_003: {{route_eml_003.output}}
- eml_004: {{route_eml_004.output}}
请汇总（一句中文）。"""


def build_triage_workflow() -> Workflow:
    """构建邮件分拣 workflow。

    classify 和 summarize 用 ``wf.llm_step`` —— 框架级 LLM 调用，prompt 里
    的 ``{{upstream.output}}`` 模板由 ``Plan.resolve_params`` 运行时解析。
    route 用 ``wf.tool_step`` 引用已注册工具，HIGH side-effect 走 HITL。
    """
    wf = Workflow()

    @wf.step
    async def read_inbox_step() -> dict:
        return _value(await read_inbox())

    # 每封邮件一个 classify llm_step，4 个并行，都依赖 read_inbox_step。
    # prompt 里的 {{read_inbox_step.output}} 由 Plan.resolve_params 运行时
    # 解析成 read_inbox_step 的输出。
    for eid in _EMAIL_IDS:
        wf.llm_step(
            name=f"classify_{eid}",
            prompt=_classify_prompt(eid),
            system=_CLASSIFY_SYSTEM,
            depends_on=["read_inbox_step"],
        )

    # 每封邮件一个 route step —— 用 tool_step 让 action 直接是已注册工具
    # 的名字。PlanExecutor 通过 ToolDispatcher 调它，HIGH side-effect
    # （delete_email）触发 ApprovalHooks。
    for eid in _EMAIL_IDS:
        tool_name = _ROUTE_TABLE[eid]
        wf.tool_step(
            name=f"route_{eid}",
            tool_name=tool_name,
            params={"email_id": eid},
            depends_on=[f"classify_{eid}"],
        )

    # summarize —— terminal llm_step，prompt 绑定 4 个 route 的输出。
    wf.llm_step(
        name="summarize",
        prompt=_SUMMARIZE_PROMPT,
        system=_SUMMARIZE_SYSTEM,
        depends_on=[f"route_{eid}" for eid in _EMAIL_IDS],
        is_terminal=True,
    )

    return wf
