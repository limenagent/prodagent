"""03 深度研究 —— 连查很多轮也不撑爆窗口：五级压缩是一种可替换策略。

上下文窗口不是内存，每次调用模型前都现场装配。这里用五级压缩：窗口够就不动，
超了先机械缩短工具结果（不花模型钱），再逐级摘要，只有摘要级才调用一次压缩模型。
换一套压缩策略，只换这个注入对象，内核与 Agent 都不用改。

跑法：PYTHONPATH=. python3 examples/03_deep_research.py
"""

import asyncio

from src import Agent
from src.kernel import LlmReply, ToolCall
from src.runtime.context import CompressionLevel, TieredCompactionContext
from src.runtime.llm import ScriptedLlm, env_llm


class ConstSummarizer:
    """固定返回一段摘要的模型，专门扮演“压缩器”，并统计被调用次数。"""

    def __init__(self):
        self.times = 0

    async def chat(self, messages, tools=None, system=None):
        self.times += 1
        return LlmReply(text="（早期检索要点：市场规模、增速、主要玩家）")


async def main():
    async def search(query, ctx):
        """检索资料。"""
        return f"关于「{query}」的检索结果：一条带数字的资料……"

    context = TieredCompactionContext(ConstSummarizer(), capacity=6)

    agent = Agent(
        name="researcher",
        model=env_llm(
            ScriptedLlm(
                [
                    ToolCall("search", {"query": "市场规模"}),
                    ToolCall("search", {"query": "年增速"}),
                    ToolCall("search", {"query": "头部玩家"}),
                    ToolCall("search", {"query": "政策风向"}),
                    "报告：综合四轮检索，市场规模稳步增长，头部集中，政策友好……",
                ]
            )
        ),
        instruction="你是行业研究员。",
        tools=[search],
        context=context,
    )

    result = await agent.run("帮我研究新能源赛道")
    print("最终报告：", result.output)
    print(
        f"检索 {result.metrics['tool_calls']} 轮｜压缩到级别 "
        f"{CompressionLevel.NAME[context.last_level]}｜摘要模型调用 {context.summarizer.times} 次"
    )


if __name__ == "__main__":
    asyncio.run(main())
