"""邮件分拣 FakeLLM —— 按 system prompt 路由到 per-agent 队列。

三类调用方共享一个 FakeLLM 实例,按 system prompt 内容分发:

  - workflow 的 ``classify_*`` llm_step  → system 含 "你是邮件分类器"
  - workflow 的 ``summarize`` llm_step   → system 含 "你是邮件分拣汇总器"
  - 主 agent ``email_triage`` (REACTIVE) → system 含 "邮件分拣编排 agent"

主 agent REACTIVE 轨迹:
  - 首次调用(无 tool result) → 发 ``spawn_agent(name="triage_workflow")``
  - spawn 返回后(有 tool result) → 把汇总讲给用户
  - 用户追问(有历史 tool result 但本轮是 user 消息) → 对话回答,不重跑 DAG

workflow 的 4 个 ``classify_*`` llm_step 并行执行,调用顺序不定;线性
``script()`` 没法处理并行顺序,所以用 RoutingFakeLLM 按 system prompt
分发。
"""

from __future__ import annotations

import asyncio
from typing import Any

from prodagent.core.types import LLMResponse, MessageList, ToolCall
from prodagent.llm.base import LLMClient, LLMConfig

_CLASSIFY_RESPONSES: list[LLMResponse] = [
    LLMResponse(
        content='{"category": "newsletter", "suggested_action": "archive_email"}',
        stop_reason="end_turn",
        input_tokens=50,
        output_tokens=20,
    ),
    LLMResponse(
        content='{"category": "action_needed", "suggested_action": "mark_read"}',
        stop_reason="end_turn",
        input_tokens=50,
        output_tokens=20,
    ),
    LLMResponse(
        content='{"category": "phishing", "suggested_action": "delete_email"}',
        stop_reason="end_turn",
        input_tokens=50,
        output_tokens=20,
    ),
    LLMResponse(
        content='{"category": "notification", "suggested_action": "archive_email"}',
        stop_reason="end_turn",
        input_tokens=50,
        output_tokens=20,
    ),
]

_SUMMARIZE_RESPONSE = LLMResponse(
    content=(
        "分拣完成。归档了 eml_001(newsletter)和 eml_004(notification),"
        "标记 eml_002(action-needed)为已读,删除 eml_003(phishing,"
        "经 HITL 门禁批准)。"
    ),
    stop_reason="end_turn",
    input_tokens=80,
    output_tokens=40,
)

_REPLAN_FALLBACK = LLMResponse(
    content='{"steps": []}',
    stop_reason="end_turn",
    input_tokens=10,
    output_tokens=5,
)


def _spawn_triage_workflow_call() -> LLMResponse:
    """主 agent 首次调用 → 委派给 triage_workflow 子 agent。"""
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(
            name="spawn_agent",
            params={
                "name": "triage_workflow",
                "task": "分拣收件箱: 归档 newsletter,标记 action-needed 为已读,删除 phishing。",
            },
        )],
        stop_reason="tool_use",
    )


def _main_agent_summary() -> LLMResponse:
    """主 agent 拿到 spawn 结果后 → 把分拣汇总讲给用户。"""
    return LLMResponse(
        content=(
            "分拣完成。归档了 eml_001(newsletter)和 eml_004(notification),"
            "标记 eml_002(action-needed)为已读,删除 eml_003(phishing,"
            "经 HITL 门禁批准)。你可以追问某封邮件的细节,或要求重新分拣。"
        ),
        stop_reason="end_turn",
    )


def _followup_response(user_msg: str) -> LLMResponse:
    """主 agent 被追问 → 基于已有分拣结果对话回答,不重跑 DAG。"""
    if "eml_003" in user_msg and ("为什么" in user_msg or "删" in user_msg):
        content = (
            "eml_003 被删除是因为它被分类为 phishing —— 发件人 "
            "suspicious@phish.example,主题 'Urgent: verify your account',"
            "正文要求点击验证链接,典型钓鱼特征。delete_email 是 HIGH 副作用,"
            "走了 HITL 门禁批准后删除。"
        )
    elif "eml_002" in user_msg and "归档" in user_msg:
        content = "好的,eml_002(action-needed,老板要你 review 的)我直接归档了。"
    elif "重分" in user_msg or "重新分拣" in user_msg or "新一批" in user_msg:
        content = "好的,我重新触发分拣 workflow 扫描最新收件箱。"
    else:
        content = (
            "基于刚才的分拣结果,eml_001/004 已归档,eml_002 已标记已读,"
            "eml_003(phishing)已删除。你想了解哪封邮件的细节?"
        )
    return LLMResponse(content=content, stop_reason="end_turn", input_tokens=50, output_tokens=30)


def _last_user_of(messages: MessageList) -> str:
    return next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"),
        "(no user message)",
    )


class TriageFakeLLM(LLMClient):
    """按 system prompt 把 complete() 分发到 per-agent 队列。"""

    def __init__(self) -> None:
        self._routes: list[tuple[str, list[LLMResponse]]] = [
            ("你是邮件分类器", list(_CLASSIFY_RESPONSES)),
            ("你是邮件分拣汇总器", [_SUMMARIZE_RESPONSE]),
        ]
        self._replan_fallback = _REPLAN_FALLBACK
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str | list[dict[str, Any]] = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: Any = None,
    ) -> LLMResponse:
        self._call_count += 1
        sys_str = system if isinstance(system, str) else str(system)

        # workflow 的 classify/summarize llm_step: 按 system marker 取队列。
        for marker, q in self._routes:
            if marker in sys_str and q:
                resp = q.pop(0)
                break
        else:
            # 主 agent email_triage: REACTIVE 轨迹,看消息历史决定下一步。
            if "邮件分拣编排 agent" in sys_str:
                last_non_assistant = next(
                    (m for m in reversed(messages) if m.get("role") != "assistant"),
                    None,
                )
                if last_non_assistant is not None and last_non_assistant.get("role") == "tool":
                    # 刚 spawn 返回 → 总结分拣结果讲给用户
                    resp = _main_agent_summary()
                else:
                    if any(m.get("role") == "tool" for m in messages):
                        # 有历史 spawn 结果,但本轮是用户追问 → 对话回答
                        resp = _followup_response(_last_user_of(messages))
                    else:
                        # 无 tool result(首轮) → 触发 spawn
                        resp = _spawn_triage_workflow_call()
            else:
                last_user = next(
                    (m["content"] for m in reversed(messages) if m.get("role") == "user"),
                    "(no user message)",
                )
                resp = LLMResponse(
                    content=f"[fallback] {last_user}",
                    stop_reason="end_turn",
                    input_tokens=50,
                    output_tokens=10,
                )

        if resp.content and on_chunk is not None:
            for word in resp.content.split():
                await on_chunk(word + " ")
                await asyncio.sleep(0)
        return resp


def build_fake_llm() -> LLMClient:
    """离线 demo 用: 主 agent + classify/summarize llm_step 全 scripted。"""
    return TriageFakeLLM()
