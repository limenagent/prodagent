"""五级压缩、技能目录加载、长期记忆召回的测试。"""

import asyncio

import pytest

from src import Agent
from src.kernel import LlmReply, ToolCall
from src.runtime.context import TieredCompactionContext, CompressionLevel
from src.runtime.skills import SkillRegistry
from src.runtime.memory import InMemoryMemory


class FixedSummarizer:
    """统计被调用次数的摘要器。"""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None, system=None, on_delta=None):
        self.calls += 1
        return LlmReply(text="要点摘要")


def _chat(n):
    msgs = [{"role": "user", "content": "开头诉求"}]
    for i in range(n):
        msgs.append({"role": "assistant", "content": f"第{i}轮回复"})
        msgs.append({"role": "user", "content": f"第{i}轮追问"})
    return msgs


async def test_tiered_no_compression_when_fits():
    ctx = TieredCompactionContext(FixedSummarizer(), capacity=20)
    out = await ctx.assemble(_chat(3))
    assert ctx.last_level == CompressionLevel.NONE
    assert len(out) == len(_chat(3))


async def test_tiered_tool_compress_spends_no_llm():
    summ = FixedSummarizer()
    ctx = TieredCompactionContext(summ, capacity=8)
    msgs = _chat(5)                       # 11 条，ratio≈1.4 → 工具压缩级
    out = await ctx.assemble(msgs)
    assert ctx.last_level == CompressionLevel.TOOL_COMPRESS
    assert summ.calls == 0                # 机械级不花模型钱
    assert len(out) <= 8


async def test_tiered_history_summary_spends_one_llm():
    summ = FixedSummarizer()
    ctx = TieredCompactionContext(summ, capacity=8)
    out = await ctx.assemble(_chat(9))    # 19 条，ratio≈2.4 → 历史摘要级
    assert ctx.last_level == CompressionLevel.HISTORY_SUMMARY
    assert summ.calls == 1
    assert any("历史摘要" in str(m.get("content", "")) for m in out)
    assert len(out) <= 8


async def test_tiered_emergency_keeps_only_tail():
    summ = FixedSummarizer()
    ctx = TieredCompactionContext(summ, capacity=8)
    out = await ctx.assemble(_chat(20))   # 41 条，ratio≈5 → 紧急级
    assert ctx.last_level == CompressionLevel.EMERGENCY
    assert len(out) <= 8


async def test_tiered_never_orphans_tool_result():
    summ = FixedSummarizer()
    ctx = TieredCompactionContext(summ, capacity=6)
    msgs = [{"role": "user", "content": "任务"}]
    for i in range(6):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [ToolCall("t", {"i": i})]})
        msgs.append({"role": "tool", "name": "t", "content": f"结果{i}" * 40})
    out = await ctx.assemble(msgs)
    # 任何保留下来的 tool 结果，前面都必须能找到携带 tool_calls 的 assistant。
    for i, m in enumerate(out):
        if m.get("role") == "tool":
            assert any(x.get("tool_calls") for x in out[:i])


def test_skill_load_from_dir(tmp_path):
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: 演示技能\ndescription: 用来测试加载\n---\n第一步这样\n第二步那样\n",
        encoding="utf-8")
    reg = SkillRegistry()
    loaded = reg.load_dir(str(tmp_path))
    assert len(loaded) == 1
    skill = reg.resolve("演示技能")
    assert skill is not None
    assert "第一步这样" in skill.instructions


class _RecordingLlm:
    def __init__(self):
        self.last_system = None

    async def chat(self, messages, tools=None, system=None, on_delta=None):
        self.last_system = system
        return LlmReply(text="已按偏好处理")


async def test_memory_recall_injected_into_system():
    memory = InMemoryMemory()
    await memory.remember("用户点奶茶的偏好：无糖去冰", tags=["偏好"])
    llm = _RecordingLlm()
    agent = Agent("regular", model=llm, instruction="你是点单助手。", memory=memory)
    result = await agent.run("帮我点杯奶茶")
    assert result.output == "已按偏好处理"
    assert llm.last_system and "无糖去冰" in llm.last_system
