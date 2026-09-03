"""Deep Research —— REACTIVE 多轮探索 + context 压缩。

本示例展示:
  - ``REACTIVE`` 多轮探索: 每 turn LLM 发一个 tool_call,看结果决定下一步。
    「fetch → 读内容 → 记数字 → fetch 下一页 → 综合」的线性探索流程。
  - ``ContextManager`` 压缩: 长跑后历史 + 工具结果累积超阈值,框架自动从
    NONE → TOOL_COMPRESS → HISTORY_SUMMARY 压缩,早期对话被总结,LLM 不丢
    关键 claim。demo 把 ``max_tokens`` 调低让压缩早触发。
    ``emergency_at=1.0`` 关掉 EmergencyStage(小窗口下它的 fit_budget 会清空
    history → LLM 死循环);``topic_summary_at=0.95`` 抬高,fake LLM 下不真
    触发(避免 aux call 共享队列吃掉 scripted turn)。
  - ``SkillRegistry``: ``deep-research.md`` runbook,LLM ``get_skill`` 学探索流程。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from prodagent import Agent, AgentConfig, ContextConfig, FrameworkConfig, HardBudget, use_fake_llm
from prodagent.skills.registry import SkillRegistry

from deep_research.fake_llm import build_fake_llm
from deep_research.tools import synthesize_report, web_fetch

_BASE = Path(__file__).parent
SKILLS_DIR = _BASE / "skills"

_SYSTEM_PROMPT = """\
你是深度研究 agent。研究是探索性的 —— 你不知道下一步要搜什么,直到你看完
上一步的结果。所以用 REACTIVE 多轮探索,不是一次性 plan:

1. 先调 ``get_skill(name="deep-research")`` 加载研究 runbook。
2. 按 runbook 依次 fetch 每个 URL → 读 content → 记关键数字。
3. 够了就 ``synthesize_report`` 产出带 [1][2] 引用的 markdown 报告。

## 关键规则
- **mock web**:只能 fetch 下面这 5 个 URL。凭记忆硬编码真实世界 URL
  (anthropic.com 等)会 404。
    - example.com/gpt4o-bench · example.com/claude35-bench ·
      example.com/third-party-bench · example.com/humaneval-deep-dive ·
      example.com/tool-use-comparison
- **每个 URL 只 fetch 一次**。读完进下一步,绝不重 fetch。
- **claim 只能用 source 里真实存在的数字**。别为凑来源数硬编。
- fetch 结果直接进 context(不 spill),读 content 字段,不需要 read_tool_result。
- 长跑后 context 自动压缩,关键 claim 不丢。
"""


def _build_framework_config() -> FrameworkConfig:
    """demo 专调:调小 context window 让压缩早触发。

    8K window(默认 100K 的 1/12,省 token)+ 5 页 × ~1000 tok fetch 结果累积,
    真 LLM 模式下 ratio 逐轮爬坡,依次触发 TOOL_COMPRESS → HISTORY_SUMMARY。
    ``tool_compress_at=0.20``(~1600 tok):第 2 次 fetch 后命中,工具结果被
    规则压缩(head/tail hint)。``history_summary_at=0.40``(~3200 tok):第 3-4 次
    fetch 后命中,早期对话被 LLM 总结。两级阈值拉开让分级可见。

    ``emergency_at=1.0`` 关掉 EmergencyStage —— 它的 ``should_skip`` 永远
    返回 False(后备 stage),只要 ratio 到达阈值就选它。小窗口下它的
    ``fit_budget`` 要同时装 SUMMARY 块 + 最近 2 条消息(含 tool_use/tool_result
    pair),预算不够就返回空 list → LLM 拿到空历史 → 死循环。抬到 1.0 后,
    TOPIC_SUMMARY 成为链尾:ratio 再高也只是让 TOPIC_SUMMARY 继续 skip,
    所有 stage 都 skip 时 ``HistoryCompressor`` 回退到 ``fit_budget`` 截尾
    (保留 SUMMARY + 尾部消息),不会出现清空。

    ``topic_summary_at=0.95`` 抬高:fake LLM 下 TOPIC_SUMMARY 调 LLM 做
    summary,aux call 会共享 FakeLLM 队列吃掉 scripted turn(已知问题)。
    真 LLM 模式可调回 0.72 让 TOPIC_SUMMARY 也触发。

    ``spill_preview_chars=10_000`` 大于最大页面(~4700 字),确保 fetch 结果
    不 spill,LLM 直接读 content 字段里的数字。
    """
    ctx = ContextConfig(
        max_tokens=8_000,
        tool_compress_at=0.20,
        history_summary_at=0.40,
        topic_summary_at=0.95,
        emergency_at=1.0,
        spill_preview_chars=10_000,
    )
    return FrameworkConfig(context=ctx)


DEFAULT_TASK = "对比 GPT-4o 和 Claude 3.5 在代码任务上的能力差异,引用 5 个独立来源。"


def build_deep_research_agent(
    *,
    framework_config: FrameworkConfig | None = None,
) -> Agent:
    """组装 Deep Research Agent。

    Args:
        framework_config: 父 fw;不传时用 demo 专调的 ``_build_framework_config``
            (调小 context 阈值让压缩早触发)。playground 注入带独立 namespace 的 fw,
            但 demo 必须用调小的 context 配置才能触发压缩 —— 这里 override context,
            保留传入 fw 的 backend(namespace 隔离 + 连接池复用)。
    """
    demo_fw = _build_framework_config()
    demo_fw.profile = "production"  # this example demos the compression stack
    if framework_config is not None:
        fw = dataclasses.replace(framework_config, context=demo_fw.context)
    else:
        fw = demo_fw
    skills = SkillRegistry.from_dir(SKILLS_DIR)
    use_fake = use_fake_llm()
    llm = build_fake_llm() if use_fake else None

    return Agent(
        "deep_research",
        system_prompt=_SYSTEM_PROMPT,
        tools=[web_fetch, synthesize_report],
        budget=HardBudget(max_turns=30, max_cost_usd=1.0, max_seconds=600.0),
        config=AgentConfig(
            name="deep_research",
            skills=skills,
            llm=llm,
            framework=fw,
        ),
    )
