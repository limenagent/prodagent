"""Deep research — many search rounds without blowing the window, then a report.

Two lessons in one flow:
- **five-level compaction** is a replaceable strategy. The context window is
  assembled fresh before every model call: untouched while it fits; past
  capacity, tool results are mechanically shortened first (no model spend), then
  summarized level by level — only the summary levels call the compressor model.
- the **artifact law, both directions**: writing, the study's bytes go to the
  BlobStore and a pointer fact to the stream (it shows in the Files panel and
  survives replay); reading, a later node folds the pointer back, fetches the
  bytes, and feeds one slice to a model — archived bytes reach the LLM only
  through such a read, never riding in the messages themselves.

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
            "verify_q": "The archived report opens with:\n{line}\nWhat is it about? One sentence.",
            "verify_a": "A sector report: the market grows steadily, players concentrate, policy is friendly.",
        },
        "zh": {
            "instruction": "你是行业研究员。",
            "search_fmt": "关于「{query}」的检索结果：一条带数字的资料……",
            "summary": "（早期检索要点已压缩：市场规模、增速、主要玩家）",
            "queries": ["市场规模", "年增速", "头部玩家", "政策风向"],
            "final": "报告：综合四轮检索，市场规模稳步增长，头部集中，政策友好……",
            "artifact": "new-energy-report.md",
            "artifact_title": "新能源赛道研究报告",
            "verify_q": "存档报告的开头是：\n{line}\n它讲的是什么？一句话回答。",
            "verify_a": "一份行业报告：新能源市场稳步增长、头部集中、政策友好。",
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

    async def verify_report(saved, ctx):
        """The read side of the artifact law: fold the pointer from this run's
        facts, fetch the bytes, feed one slice to a model — the archive lives
        again as context, while the messages stay small."""
        report = await ctx.load_artifact(t["artifact"])
        line = report.decode().strip().splitlines()[0]
        answer = await ctx.llm_complete(t["verify_q"].format(line=line))
        return f"{saved}; read back {len(report)} bytes — {answer}"

    wf = Workflow(model=env_llm(ScriptedLlm([t["verify_a"]])))
    wf.add_node("researcher", researcher)
    wf.add_node("write_report", write_report)
    wf.add_node("verify_report", verify_report, terminal=True)
    wf.add_edge("researcher", "write_report")
    wf.add_edge("write_report", "verify_report")
    wf.entry("researcher")
    return wf


async def main():
    r = await build("en").run("Research the new-energy sector for me")
    print("Final:", r.output)
    print(f"waves: {r.metrics['waves']}")


if __name__ == "__main__":
    asyncio.run(main())
