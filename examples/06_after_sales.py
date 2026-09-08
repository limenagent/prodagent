"""06 After-sales refund — a supervisor agent whose "tools" are other agents.

The supervisor never executes anything itself. Each teammate is registered as
a delegation tool: calling it runs that agent's own loop — its instruction,
its tools, its context — and returns only the final answer (the call
semantics: dispatched out, result comes back). The risk specialist is itself
a supervisor of a smaller expert, so one run grows a three-level delegation
tree; every level runs the same loop, there is no multi-agent engine.

Each specialist sees only its own read-only tools: the tool set is the
permission boundary between roles.

Run: PYTHONPATH=. python3 examples/06_after_sales.py
"""

import asyncio

from src import Agent
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm


async def main():
    # Read-only business tools — one system per specialist, nothing shared.
    async def query_invoice(order_id, ctx):
        """Read the invoice and payment status of an order."""
        return f"{order_id}: charged ¥399 on Sep 2, shipment never dispatched"

    async def query_blacklist(order_id, ctx):
        """Check whether an order touches any blacklisted account."""
        return f"{order_id}: buyer clean, no blacklist hits"

    async def query_related(order_id, ctx):
        """List accounts related to the buyer of an order."""
        return f"{order_id}: 1 related account, dormant, no fraud record"

    # Level three: a mini-expert the risk specialist delegates to in turn.
    # (ScriptedLlm plays every model so the demo runs offline; swap in a real
    # model when ready.)
    related = Agent(
        "related",
        model=env_llm(
            ScriptedLlm(
                [
                    ToolCall("query_related", {"order_id": "O-1234"}),
                    "One related account, dormant for 2 years, no fraud record.",
                ]
            )
        ),
        instruction="You analyze accounts related to a buyer; answer in one sentence.",
        tools=[query_related],
    )

    risk = Agent(
        "risk",
        model=env_llm(
            ScriptedLlm(
                [
                    ToolCall("query_blacklist", {"order_id": "O-1234"}),
                    ToolCall("related", {"task": "check accounts related to order O-1234"}),
                    "Blacklist clean; the one related account is dormant — risk is low.",
                ]
            )
        ),
        instruction="You judge fraud and credit risk; check the data before concluding.",
        tools=[query_blacklist],
        teammates=[related],
    )

    billing = Agent(
        "billing",
        model=env_llm(
            ScriptedLlm(
                [
                    ToolCall("query_invoice", {"order_id": "O-1234"}),
                    "Charged ¥399 on Sep 2 and the shipment never went out; refund due in full.",
                ]
            )
        ),
        instruction="You answer billing facts only: invoices, payments, refunds.",
        tools=[query_invoice],
    )

    # The supervisor's script reads exactly like tool use — except the two
    # "tools" it calls are the specialists above.
    supervisor = Agent(
        "supervisor",
        model=env_llm(
            ScriptedLlm(
                [
                    ToolCall("billing", {"task": "gather the billing facts of order O-1234"}),
                    ToolCall("risk", {"task": "assess the fraud risk of order O-1234"}),
                    "Billing confirms charged-but-unshipped and risk is low: "
                    "approve a full ¥399 refund.",
                ]
            )
        ),
        instruction=(
            "You are the after-sales supervisor. You never execute yourself: "
            "dispatch the right specialist, wait for the answer, then decide."
        ),
        teammates=[billing, risk],
    )

    result = await supervisor.run("Order O-1234: customer charged but nothing shipped. Refund?")
    print("Decision:", result.output)
    print()
    print("What each delegation brought back:")
    for m in result.messages:
        if m.get("role") == "tool":
            print(f"  {m['name']} -> {m['content']}")
    print()
    print(
        f"supervisor: {result.metrics['llm_calls']} thinks, "
        f"{result.metrics['tool_calls']} delegations — each specialist ran its own "
        "loop underneath"
    )


if __name__ == "__main__":
    asyncio.run(main())
