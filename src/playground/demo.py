"""Built-in CLI demo: customer-support refund with an approval gate.

A support agent first queries the order with a read-only tool and proposes a
refund; when money is about to move, the flow suspends at an ``approve`` node via
``wait_human`` and resumes after a person decides — Approve executes the refund,
Reject closes it. The agent is a node in the Workflow and runs on the same
scheduler, so its events feed the run automatically (no bus wiring).

- with OPENAI_API_KEY set, pass a real model to ``build_demo(model)``;
- without it, the built-in scripted model runs the whole loop offline.
"""

from __future__ import annotations

from src import Agent, Workflow, go, wait_human
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm

# A pretend order store; a real project would query a database / downstream API.
_ORDERS = {
    "O-1234": {"status": "not shipped for 3 days (overdue)", "amount": 88},
    "O-5678": {"status": "delivered", "amount": 120},
}


async def query_order(order_id, ctx):
    """Look up an order's status and amount by order id."""
    order = _ORDERS.get(order_id, {"status": "no such order", "amount": 0})
    return f"Order {order_id}: {order['status']}, amount {order['amount']}"


async def _refund(suggestion, ctx):
    return f"Refund executed per the approval. {suggestion}"


async def _deny(suggestion, ctx):
    return f"Approval denied; refund closed and the user was notified. {suggestion}"


def _scripted_model():
    # Query the order first, then propose — the two turns a real model would take.
    return env_llm(
        ScriptedLlm(
            [
                ToolCall("query_order", {"order_id": "O-1234"}),
                "Order O-1234 has not shipped for 3 days; per policy I suggest refunding 88.",
            ]
        )
    )


def build_demo(model=None) -> Workflow:
    model = model or _scripted_model()
    wf = Workflow()
    support = Agent(
        name="support",
        model=model,
        instruction="You are an after-sales support agent. First check the order with "
        "query_order, then judge whether a refund is due. State your suggestion and the "
        "amount in one sentence; never execute a refund on your own.",
        tools=[query_order],
    )

    async def approve(suggestion, ctx):
        if ctx.resume_value is None:  # first time: really stop and ask
            return wait_human(
                "This order is eligible for a refund. Approve?", {"suggestion": suggestion}
            )
        target = "refund" if ctx.resume_value.get("approved") else "deny"
        return go(target, suggestion, decision=target)

    wf.add_node("support", support)
    wf.add_node("approve", approve)
    wf.add_node("refund", _refund, terminal=True)
    wf.add_node("deny", _deny, terminal=True)
    wf.add_edge("support", "approve")
    # Mutually exclusive branches: only the node `decision` points at activates;
    # the unchosen terminal is swept as skipped.
    wf.branch(
        "approve",
        {"refund": "refund", "deny": "deny"},
        decide=lambda s: s.get("decision"),
    )
    wf.entry("support")
    return wf
