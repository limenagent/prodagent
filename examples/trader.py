"""Bubble-tea proxy buy — multi-round haggling, then a human approval gate.

- ``quote`` is a read-only tool, called repeatedly as the buyer haggles down;
- once haggled, the flow suspends at an approval node (``wait_human``) and only
  continues after a person decides — Approve places the order, Reject abandons;
- the buyer is an Agent node inside a Workflow: it runs its own ReAct loop, then
  hands the haggled plan to the approval node.

``build(lang)`` is the single assembly point (bilingual); ``main`` runs English
and auto-approves so the script shows the whole flow. In the playground you click
Approve/Reject yourself.

Run: PYTHONPATH=. python3 examples/trader.py
"""

import asyncio

from src import Agent, Workflow, go, wait_human
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm
from src.runtime.memory import InMemoryMemory


def build(lang: str = "en") -> Workflow:
    t = {
        "en": {
            "instruction": "You are a purchasing agent: haggle first, then request order approval.",
            "quote_fmt": "current quote: ¥{v}",
            "order_fmt": "Order placed: {plan}",
            "cancel_fmt": "No deal on price; order abandoned: {plan}",
            "question": "Haggled down to ¥14, self pickup. Approve the order?",
            "script": [
                ToolCall("quote", {}),
                ToolCall("quote", {}),
                "After two rounds of haggling: ¥14, self pickup. Ready to order.",
            ],
        },
        "zh": {
            "instruction": "你是代购助手，先砍价再申请下单。",
            "quote_fmt": "当前报价 {v} 元",
            "order_fmt": "订单已下：{plan}",
            "cancel_fmt": "价格没谈拢，已放弃下单：{plan}",
            "question": "代购谈到 14 元自取，批准下单吗？",
            "script": [
                ToolCall("quote", {}),
                ToolCall("quote", {}),
                "两轮砍价后谈到 14 元自取，准备下单。",
            ],
        },
    }[lang]
    price = {"v": 20}

    async def quote(ctx):
        """Ask the seller for the current price."""
        price["v"] -= 2
        return t["quote_fmt"].format(v=price["v"])

    async def place_order(plan, ctx):
        return t["order_fmt"].format(plan=plan)

    async def cancel(plan, ctx):
        return t["cancel_fmt"].format(plan=plan)

    wf = Workflow()
    buyer = Agent(
        name="buyer",
        model=env_llm(ScriptedLlm(list(t["script"]))),
        instruction=t["instruction"],
        tools=[quote],
        memory=InMemoryMemory(),
    )

    async def approve(plan, ctx):
        if ctx.resume_value is None:
            return wait_human(t["question"], {"plan": plan})
        target = "place_order" if ctx.resume_value.get("approved") else "cancel"
        return go(target, plan, decision=target)

    wf.add_node("buyer", buyer)
    wf.add_node("approve", approve)
    wf.add_node("place_order", place_order, terminal=True)
    wf.add_node("cancel", cancel, terminal=True)
    wf.add_edge("buyer", "approve")
    # Mutually exclusive branches: only the node `decision` points at activates.
    wf.branch(
        "approve",
        {"place_order": "place_order", "cancel": "cancel"},
        decide=lambda s: s.get("decision"),
    )
    wf.entry("buyer")
    return wf


async def main():
    wf = build("en")
    r = await wf.run("Buy me a bubble tea, as cheap as you can")
    if str(r.run.state) == "suspended":  # auto-approve the gate for the demo
        r = await wf.resume(r.run_id, {"approved": True})
    print("Final:", r.output)
    print(f"waves: {r.metrics['waves']}")


if __name__ == "__main__":
    asyncio.run(main())
