"""Built-in playground demo: customer-support refund.

A path that shows off the framework: a support agent first queries the order
with a read-only tool and proposes a refund; when money is about to move, the
flow suspends at an `approve` node via wait_human, and the web page resumes it
after Approve/Reject.

- With OPENAI_API_KEY set: a real OpenAI-compatible model is used;
- Without it: the built-in scripted model runs the demo offline — zero config,
  and the full "chat → approval → continue" loop still plays end to end.
"""

from __future__ import annotations

from src import Agent, Workflow, go, wait_human
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm

# A pretend order store; a real project would query a database / downstream service.
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


def _default_model():
    # Offline script: query the order first, then propose — matches the two
    # turns a real model would take.
    return ScriptedLlm(
        [
            ToolCall("query_order", {"order_id": "O-1234"}),
            "Order O-1234 has not shipped for 3 days; per policy I suggest refunding 88.",
        ]
    )


def build_demo(model=None) -> Workflow:
    model = model or env_llm(_default_model())
    wf = Workflow(model=model)
    support = Agent(
        name="support",
        model=model,
        instruction="You are an after-sales support agent. First check the order with "
        "query_order, then judge whether a refund is due. State your suggestion and "
        "the amount in one sentence; never execute a refund on your own.",
        tools=[query_order],
        bus=wf.bus,
    )  # the child agent's events feed the same bus

    async def approve(suggestion, ctx):
        if ctx.resume_value is None:
            # First time here: nobody has been asked yet — really stop and hand
            # the suggestion out for approval.
            return wait_human(
                "This order is eligible for a refund. Approve?", {"suggestion": suggestion}
            )
        if ctx.resume_value.get("approved"):
            return go("refund", suggestion, decision="approved")
        return go("deny", suggestion, decision="denied")

    wf.add_node("support", support)
    wf.add_node("approve", approve)
    wf.add_node("refund", _refund, terminal=True)
    wf.add_node("deny", _deny, terminal=True)
    wf.add_edge("support", "approve")
    # Mutually exclusive terminals, gated by the resume decision. As plain
    # static edges both would fire the moment `approve` completes, so refund AND
    # deny would run; the `when` guards light up only the branch the human chose
    # (the unchosen terminal is swept as skipped). Same shape as the trader demo.
    wf.add_edge("approve", "refund", when=lambda s: s.get("decision") == "approved")
    wf.add_edge("approve", "deny", when=lambda s: s.get("decision") == "denied")
    wf.entry("support")
    return wf
