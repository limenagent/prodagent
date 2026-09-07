"""Tests for the five compaction levels, skill directory loading, and long-term memory recall."""

from src import Agent
from src.kernel import LlmReply, ToolCall
from src.runtime.context import CompressionLevel, TieredCompactionContext
from src.runtime.memory import InMemoryMemory
from src.runtime.skills import SkillRegistry


class FixedSummarizer:
    """A summarizer that counts how many times it was called."""

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
    msgs = _chat(5)  # 11 messages, ratio~1.4 -> tool-compress level
    out = await ctx.assemble(msgs)
    assert ctx.last_level == CompressionLevel.TOOL_COMPRESS
    assert summ.calls == 0  # mechanical level, spends no model calls
    assert len(out) <= 8


async def test_tiered_history_summary_spends_one_llm():
    summ = FixedSummarizer()
    ctx = TieredCompactionContext(summ, capacity=8)
    out = await ctx.assemble(_chat(9))  # 19 messages, ratio≈2.4 -> history-summary level
    assert ctx.last_level == CompressionLevel.HISTORY_SUMMARY
    assert summ.calls == 1
    assert any("History summary" in str(m.get("content", "")) for m in out)
    assert len(out) <= 8


async def test_tiered_emergency_keeps_only_tail():
    summ = FixedSummarizer()
    ctx = TieredCompactionContext(summ, capacity=8)
    out = await ctx.assemble(_chat(20))  # 41 messages, ratio~5 -> emergency level
    assert ctx.last_level == CompressionLevel.EMERGENCY
    assert len(out) <= 8


async def test_tiered_never_orphans_tool_result():
    summ = FixedSummarizer()
    ctx = TieredCompactionContext(summ, capacity=6)
    msgs = [{"role": "user", "content": "任务"}]
    for i in range(6):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [ToolCall("t", {"i": i})]})
        msgs.append({"role": "tool", "name": "t", "content": f"结果{i}" * 40})
    out = await ctx.assemble(msgs)
    # any tool result that survives must have a preceding assistant message carrying tool_calls.
    for i, m in enumerate(out):
        if m.get("role") == "tool":
            assert any(x.get("tool_calls") for x in out[:i])


def test_skill_load_from_dir(tmp_path):
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: 演示技能\ndescription: 用来测试加载\n---\n第一步这样\n第二步那样\n",
        encoding="utf-8",
    )
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
