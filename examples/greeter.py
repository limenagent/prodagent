"""Greet & order — the smallest agent: one model + one tool, just run it.

Run: PYTHONPATH=. python3 examples/greeter.py

``build(lang)`` is the single assembly point (bilingual); ``main`` just runs the
English script and prints. With OPENAI_API_KEY set a real model is used,
otherwise ScriptedLlm plays the loop offline — think once, call one tool, answer.
"""

import asyncio

from src import Agent
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm


def build(lang: str = "en") -> Agent:
    t = {
        "en": {
            "instruction": "You are a bubble-tea shop assistant; keep answers short.",
            "menu": {"taro-bubble-tea": "in stock, ¥18", "americano": "in stock, ¥12"},
            "missing": "not on the menu",
            "script": [
                ToolCall("menu", {"drink": "taro-bubble-tea"}),
                "Yes — taro bubble tea is in stock, ¥18 a cup. Want me to order one?",
                "Done! One taro bubble tea for you, no sugar, less ice, as usual.",
            ],
        },
        "zh": {
            "instruction": "你是奶茶店助手，回答简洁。",
            "menu": {"芋泥啵啵": "在售，18 元", "美式": "在售，12 元"},
            "missing": "菜单里没有",
            "script": [
                ToolCall("menu", {"drink": "芋泥啵啵"}),
                "有的，芋泥啵啵在售，18 元一杯，需要帮你下单吗？",
                "好嘞，已帮你下一杯芋泥啵啵，按你的偏好无糖去冰。",
            ],
        },
    }[lang]

    async def menu(drink, ctx):
        """Check whether a drink is on the menu."""
        return t["menu"].get(drink, t["missing"])

    return Agent(
        name="greeter",
        model=env_llm(ScriptedLlm(list(t["script"]))),
        instruction=t["instruction"],
        tools=[menu],
    )


async def main():
    result = await build("en").run("Do you have taro bubble tea?")
    print("Final answer:", result.output)
    print(f"LLM calls: {result.metrics['llm_calls']} | tool calls: {result.metrics['tool_calls']}")


if __name__ == "__main__":
    asyncio.run(main())
