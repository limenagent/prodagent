"""合规审计 workflow —— DAG 写死，节点智能。

DAG::

    s1 extract_transactions (tool)
      ├── s2 flag_suspicious (wf.llm_step —— 真 LLM 逐条标注可疑)
      └── s3 enrich_entity     (wf.llm_step —— 真 LLM 关联实体/历史)
    s4 submit_sar (子 agent —— 综合 s2/s3 + 调 submit_to_regulator)

s1 是纯 tool；s2/s3 用框架的 ``wf.llm_step`` —— 每个节点是一次真 LLM
调用，prompt 用 ``{{s1.output}}`` 模板拿到 s1 的交易流水。s4 是子
agent，综合标注 + 实体关联后调写工具 ``submit_to_regulator`` 提交 SAR。

崩溃恢复: poison pill 装在 s3 的 ``wf.llm_step`` LLM 调用里(见
``fake_llm.py`` 的 ``reset_crash_state``)。RUN 1 s3 崩溃；续跑时
s1/s2 的 COMPLETED 留在 event log（含两次真 LLM 标注，省下的成本可见），
只重跑 s3/s4。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prodagent.runtime.workflow import Workflow

if TYPE_CHECKING:
    from prodagent.runtime.agent import Agent


_FLAG_SYSTEM = (
    "你是反洗钱分析师。看交易流水，逐条判断可疑性，关注: 大额拆分（structuring）、"
    "壳公司收款方、加密货币兑换、规避申报阈值（$10000/$100000）的拆单。"
    "用紧凑 JSON 返回: {\"flagged\": [{\"tx_id\": \"...\", \"reason\": \"...\", "
    "\"risk\": \"low|medium|high\"}]}。不要包装在 markdown 里。"
)

_ENTITY_SYSTEM = (
    "你是反洗钱实体关联分析师。看交易流水，按收款方聚类，识别同一主体的多笔交易、"
    "壳公司命名模式（shell-co-*）、以及发送方跨账户的行为。"
    "用紧凑 JSON 返回: {\"entities\": [{\"name\": \"...\", \"tx_ids\": [\"...\"], "
    "\"total_amount\": <n>, \"pattern\": \"...\"}]}。不要包装在 markdown 里。"
)

_FLAG_PROMPT = (
    "标注这段交易流水中每笔交易的可疑性:\n\n"
    "{{s1.output}}"
)

_ENTITY_PROMPT = (
    "关联这段交易流水中的实体，识别异常聚类:\n\n"
    "{{s1.output}}"
)


def build_audit_workflow(sar_submitter: Agent) -> Workflow:
    """构建审计 workflow。

    Args:
        sar_submitter: s4 子 agent。综合 s2/s3 的 LLM 标注后调
            ``submit_to_regulator`` 提交 SAR。poison pill 装在 s3 的
            ``wf.llm_step`` LLM 调用里(见 ``fake_llm.py``),不在写工具里。
    """
    wf = Workflow()

    # s1: 纯 tool —— 引用已在 agent 上注册的 extract_transactions（带 ToolMeta）。
    wf.tool_step("s1", "extract_transactions")

    # s2/s3: 真 LLM 标注，并行，依赖 s1 的交易流水。
    wf.llm_step("s2", _FLAG_PROMPT, system=_FLAG_SYSTEM, depends_on=["s1"])
    wf.llm_step("s3", _ENTITY_PROMPT, system=_ENTITY_SYSTEM, depends_on=["s1"])

    # s4: 子 agent —— 综合 s2/s3 + 调 submit_to_regulator。terminal。
    # task 用模板把 s2/s3 的 LLM 标注结果喂给子 agent。
    wf.step(
        sar_submitter,
        name="s4",
        depends_on=["s2", "s3"],
        is_terminal=True,
        params={"task": (
            "综合这两份 LLM 分析，调 submit_to_regulator 提交 SAR 报告。"
            "sar_summary 写一段风险叙述（哪些交易可疑、为什么），"
            "suspicious_tx_ids 填所有 risk=medium/high 的 tx_id。\n\n"
            "=== 可疑标注 ===\n{{s2.output}}\n\n"
            "=== 实体关联 ===\n{{s3.output}}"
        )},
    )

    return wf


__all__ = ["build_audit_workflow"]
