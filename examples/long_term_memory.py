"""Long-term memory — preferences recalled across turns and sessions.

Before think, the current question retrieves memories and splices them into the
system prompt; the model answers as if it remembers you. The memory bank exists
independently of this conversation's context — it is sedimented from earlier
chats, not re-derived each turn.

``build(lang)`` is the single assembly point (bilingual, async); ``main`` runs
two English turns against one agent to show recall.
Run: PYTHONPATH=. python -m examples.long_term_memory
"""

import asyncio

from src import Agent
from src.runtime.llm import ScriptedLlm, env_llm
from src.runtime.memory import InMemoryMemory


async def build(lang: str = "en") -> Agent:
    mem = InMemoryMemory()
    if lang == "en":
        await mem.remember(
            "user's tea order preference: no sugar, less ice, keep answers short",
            tags=["preference"],
        )
        script = [
            "Sure — no sugar, less ice per your saved preference; order placed.",
            "I remember — no sugar, less ice again; placing the order now.",
        ]
        instruction = "You are an ordering assistant; honor the preferences in long-term memory."
    else:
        await mem.remember("用户点奶茶的偏好：默认无糖、去冰，回答尽量简短", tags=["偏好"])
        script = [
            "好的，按你记忆中的偏好做了无糖去冰，已下单。",
            "记得呢——还是无糖去冰，这就再帮你下一单。",
        ]
        instruction = "你是点单助手，要结合长期记忆里的偏好。"
    return Agent(
        name="regular",
        model=env_llm(ScriptedLlm(script)),
        instruction=instruction,
        memory=mem,
    )


async def main():
    agent = await build("en")
    first = await agent.run("A bubble tea, please")
    print("Turn 1:", first.output)
    second = await agent.run("Another one, thanks")
    print("Turn 2:", second.output)


if __name__ == "__main__":
    asyncio.run(main())
