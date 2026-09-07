"""02 Bubble-tea proxy buy — multi-round haggling, writes through an approval
gate, long-term memory remembering preferences.

- quote is a read-only tool, callable repeatedly (it plays the back-and-forth
  of haggling);
- place_order is a side-effecting write tool: it must pass the bus
  adjudication gate before executing;
- the gate rejects the first attempt (too expensive); the rejection reason is
  fed back into the model, which revises the plan and orders again;
- memory remembers "no sugar by default" and is spliced into the system
  prompt before every think.

Run: PYTHONPATH=. python3 examples/02_trader.py
"""

import asyncio

from src import Agent
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm
from src.runtime.memory import InMemoryMemory


async def main():
    price = {"v": 20}

    async def quote(ctx):
        """Ask the seller for the current price."""
        price["v"] -= 2
        return f"current quote: ¥{price['v']}"

    async def place_order(price, pickup, ctx):
        """Place the order and pay."""
        return f"Order placed: ¥{price}, {pickup}"

    memory = InMemoryMemory()
    await memory.remember(
        "the user dislikes sweet drinks; default to no sugar", tags=["preference"]
    )

    agent = Agent(
        name="buyer",
        model=env_llm(
            ScriptedLlm(
                [
                    ToolCall("quote", {}),
                    ToolCall("quote", {}),
                    ToolCall(
                        "place_order", {"price": 16, "pickup": "delivery"}
                    ),  # gets intercepted
                    ToolCall(
                        "place_order", {"price": 14, "pickup": "self pickup"}
                    ),  # passes after revision
                    "Deal closed: ¥14, self pickup, no sugar per your preference.",
                ]
            )
        ),
        instruction="You are a buying assistant; writes must be approved first.",
        tools=[quote],
        memory=memory,
    )
    agent.add_tool(
        place_order, side_effect="write"
    )  # ordering is a write: it passes the approval gate

    # The approval gate: rejects the first attempt as too expensive, passes
    # the second once the plan is reasonable.
    gate = {"n": 0}

    def approve(**_):
        gate["n"] += 1
        return gate["n"] >= 2

    agent.bus.checker("tool:place_order", approve)

    result = await agent.run("Buy me a bubble tea, as cheap as you can")
    print("Final:", result.output)
    print(
        f"LLM calls: {result.metrics['llm_calls']} | tool calls: {result.metrics['tool_calls']} "
        "| blocked by the gate: 1"
    )


if __name__ == "__main__":
    asyncio.run(main())
