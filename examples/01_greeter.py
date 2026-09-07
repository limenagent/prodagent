"""01 Greet & order — the smallest agent: one model + one tool, just run it.

Run: PYTHONPATH=. python3 examples/01_greeter.py
ScriptedLlm plays the model from a script, so offline you can watch the full
loop of "think once, call one tool, answer".
"""

import asyncio

from src import Agent
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm


async def main():
    async def menu(drink, ctx):
        """Check whether a drink is on the menu."""
        return {
            "taro-bubble-tea": "in stock, ¥18",
            "americano": "in stock, ¥12",
        }.get(drink, "not on the menu")

    agent = Agent(
        name="greeter",
        model=env_llm(
            ScriptedLlm(
                [
                    ToolCall("menu", {"drink": "taro-bubble-tea"}),
                    "Yes — taro bubble tea is in stock, ¥18 a cup. Want me to order one?",
                ]
            )
        ),
        instruction="You are a bubble-tea shop assistant; keep answers short.",
        tools=[menu],
    )

    result = await agent.run("Do you have taro bubble tea?")
    print("Final answer:", result.output)
    print(f"LLM calls: {result.metrics['llm_calls']} | tool calls: {result.metrics['tool_calls']}")


if __name__ == "__main__":
    asyncio.run(main())
