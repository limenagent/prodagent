"""03 Deep research — search for many rounds without blowing the window:
five-level compaction is a replaceable strategy.

The context window is not memory; it is assembled fresh before every model
call. Here it is five-level compaction: untouched while it fits; past
capacity, tool results are mechanically shortened first (no model spend),
then summarized level by level — only the summary levels spend one call to
the compression model. Swap in a different strategy by swapping this one
injected object; neither the kernel nor the Agent changes.

Run: PYTHONPATH=. python3 examples/03_deep_research.py
"""

import asyncio

from src import Agent
from src.kernel import LlmReply, ToolCall
from src.runtime.context import CompressionLevel, TieredCompactionContext
from src.runtime.llm import ScriptedLlm, env_llm


class ConstSummarizer:
    """A model stub that returns one fixed summary, playing the "compressor",
    and counts how often it gets called."""

    def __init__(self):
        self.times = 0

    async def chat(self, messages, tools=None, system=None):
        self.times += 1
        return LlmReply(text="(early searches: market size, growth rate, key players)")


async def main():
    async def search(query, ctx):
        """Search for material."""
        return f"Search results for '{query}': one data-rich source…"

    context = TieredCompactionContext(ConstSummarizer(), capacity=6)

    agent = Agent(
        name="researcher",
        model=env_llm(
            ScriptedLlm(
                [
                    ToolCall("search", {"query": "market size"}),
                    ToolCall("search", {"query": "growth rate"}),
                    ToolCall("search", {"query": "top players"}),
                    ToolCall("search", {"query": "policy outlook"}),
                    "Report: across four rounds of search, the market grows steadily, "
                    "the top players concentrate, and policy is friendly…",
                ]
            )
        ),
        instruction="You are an industry researcher.",
        tools=[search],
        context=context,
    )

    result = await agent.run("Research the new-energy sector for me")
    print("Final report:", result.output)
    print(
        f"search rounds: {result.metrics['tool_calls']} | compacted to level "
        f"{CompressionLevel.NAME[context.last_level]} | compressor-model calls: {context.summarizer.times}"
    )


if __name__ == "__main__":
    asyncio.run(main())
