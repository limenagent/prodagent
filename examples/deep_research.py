"""Deep research — many search rounds without blowing the window, then a report.

Two lessons in one flow:
- **five-level compaction** is a replaceable strategy. The context window is
  assembled fresh before every model call: untouched while it fits; past
  capacity, tool results are mechanically shortened first (no model spend), then
  summarized level by level — only the summary levels call the compressor model.
- the study becomes a **versioned artifact**: bytes go to the BlobStore, a
  pointer fact to the stream, so it shows in the Files panel and survives replay.

``build(lang)`` is the single assembly point (bilingual); ``main`` runs English.
Run: PYTHONPATH=. python3 examples/deep_research.py
"""

import asyncio

from src import Agent, Workflow
from src.kernel import LlmReply, ToolCall
from src.runtime.context import TieredCompactionContext
from src.runtime.llm import ScriptedLlm, env_llm


def build(lang: str = "en") -> Workflow:
    t = {
        "en": {
            "instruction": "You are an industry researcher.",
            "search_fmt": "Search results for '{query}': one data-rich source…",
            "summary": "(early searches compacted: market size, growth rate, key players)",
            "queries": ["market size", "growth rate", "top players", "policy outlook"],
            "final": "Report: across four rounds of search, the market grows steadily, "
            "the top players concentrate, and policy is friendly…",
            "artifact": "new-energy-report.md",
            "artifact_title": "New Energy Sector Report",
        },
        "zh": {
            "instruction": "你是行业研究员。",
            "search_fmt": "关于「{query}」的检索结果：一条带数字的资料……",
            "summary": "（早期检索要点已压缩：市场规模、增速、主要玩家）",
            "queries": ["市场规模", "年增速", "头部玩家", "政策风向"],
            "final": "报告：综合四轮检索，市场规模稳步增长，头部集中，政策友好……",
            "artifact": "new-energy-report.md",
            "artifact_title": "新能源赛道研究报告",
        },
    }[lang]

    async def search(query, ctx):
        """Search for material."""
        return t["search_fmt"].format(query=query)

    class ConstSummarizer:
        async def chat(self, messages, tools=None, system=None):
            return LlmReply(text=t["summary"])

    researcher = Agent(
        name="researcher",
        model=env_llm(
            ScriptedLlm(
                [ToolCall("search", {"query": q}) for q in t["queries"]] + [t["final"]]
            )
        ),
        instruction=t["instruction"],
        tools=[search],
        context=TieredCompactionContext(env_llm(ConstSummarizer()), capacity=6),
    )

    async def write_report(text, ctx):
        pointer = await ctx.save_artifact(t["artifact"], text, title=t["artifact_title"])
        return f"Saved {pointer['filename']} (v{pointer['version']})"

    wf = Workflow()
    wf.add_node("researcher", researcher)
    wf.add_node("write_report", write_report, terminal=True)
    wf.add_edge("researcher", "write_report")
    wf.entry("researcher")
    return wf


async def main():
    r = await build("en").run("Research the new-energy sector for me")
    print("Final:", r.output)
    print(f"waves: {r.metrics['waves']}")


if __name__ == "__main__":
    asyncio.run(main())
