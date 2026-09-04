"""02 奶茶代购 —— 多轮砍价、写操作过审批门、长期记忆记住偏好。

- quote 是只读工具，可以反复调（模拟来回砍价）；
- place_order 是有副作用的写工具，执行前必须过总线裁决门；
- 裁决门第一次拒绝（太贵），拒绝原因作为反馈回喂模型，模型改方案后再下单；
- Memory 记住“默认无糖”，每次 think 前自动拼进系统提示。

跑法：PYTHONPATH=. python3 examples/02_trader.py
"""

import asyncio

from src import Agent
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm
from src.runtime.memory import InMemoryMemory


async def main():
    price = {"v": 20}

    async def quote(ctx):
        """向商家询问当前价格。"""
        price["v"] -= 2
        return f"当前报价 {price['v']} 元"

    async def place_order(price, pickup, ctx):
        """下单付款。"""
        return f"订单已下：{price} 元，{pickup}"

    memory = InMemoryMemory()
    await memory.remember("用户不爱喝甜的，默认无糖", tags=["偏好"])

    agent = Agent(
        name="buyer",
        model=env_llm(ScriptedLlm([
            ToolCall("quote", {}),
            ToolCall("quote", {}),
            ToolCall("place_order", {"price": 16, "pickup": "配送"}),    # 会被拦下
            ToolCall("place_order", {"price": 14, "pickup": "自取"}),    # 改方案后放行
            "谈妥了，14 元自取，已按你的偏好做无糖。",
        ])),
        instruction="你是代购助手，写操作必须先获批。",
        tools=[quote],
        memory=memory,
    )
    agent.add_tool(place_order, side_effect="write")   # 下单是写操作，要过审批门

    # 审批门：第一次认为太贵拒绝，第二次方案合理才放行。
    gate = {"n": 0}

    def approve(**_):
        gate["n"] += 1
        return gate["n"] >= 2
    agent.bus.checker("tool:place_order", approve)

    result = await agent.run("帮我买杯奶茶，尽量便宜点")
    print("最终：", result.output)
    print(f"模型 {result.metrics['llm_calls']} 次｜工具 {result.metrics['tool_calls']} 次｜审批拦截 1 次")


if __name__ == "__main__":
    asyncio.run(main())
