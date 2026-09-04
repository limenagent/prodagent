"""01 问候点单 —— 最小的 Agent：一个模型 + 一个工具，run 一下就好。

跑法：PYTHONPATH=. python3 examples/01_greeter.py
ScriptedLlm 按脚本扮演模型，离线就能看到“想一步、调一次工具、再回答”的完整闭环。
"""

import asyncio

from src import Agent
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm


async def main():
    async def menu(drink, ctx):
        """查询某款饮品是否在售。"""
        return {"芋泥啵啵": "在售，18 元", "美式": "在售，12 元"}.get(drink, "菜单里没有")

    agent = Agent(
        name="greeter",
        model=env_llm(ScriptedLlm([
            ToolCall("menu", {"drink": "芋泥啵啵"}),
            "有的，芋泥啵啵在售，18 元一杯，需要帮你下单吗？",
        ])),
        instruction="你是奶茶店助手，回答简洁。",
        tools=[menu],
    )

    result = await agent.run("你们这儿有芋泥啵啵吗？")
    print("最终答复：", result.output)
    print(f"模型 {result.metrics['llm_calls']} 次｜工具 {result.metrics['tool_calls']} 次")


if __name__ == "__main__":
    asyncio.run(main())
