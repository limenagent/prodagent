"""Refund routing as a handoff chain and a swarm net — control passes between
specialists and never comes back; there is no supervisor in the flow.

These are the two shapes of *transfer* — the counterpart of *call/delegation*.
Delegation (see after_sales.py) goes and comes back: a supervisor dispatches a
specialist, the answer returns, and the supervisor stays in control. A handoff
goes and does not come back: the current node hands a summary to another Agent
node with `go` and draws no return edge, so control leaves for good.

1. chain  — triage -> billing -> risk -> close, a fixed order. Each specialist
   works the case with its OWN model and tools, then hands a one-line summary
   (the go payload) to the next. After a handoff the previous node never runs
   again for this case (its attempts stay 1); nothing of its private context
   leaks forward — only the summary does.

2. swarm net — no triage, no supervisor. billing and risk may hand the case
BACK to each other when the facts do not line up (a back-edge is just another
`go` to a registered node). Whoever can finally settle it goes to close. The
next holder is chosen from the specialist's own verdict at runtime ("NEXT:.."),
not from edges drawn up front — widen the allowed peers and this is a swarm.
The scripts are finite and the kernel's max_waves backs them up, so the net
always reaches a verdict instead of bouncing forever.

The graph declares the Agent nodes but draws no edges between them: who runs
next is entirely a runtime `go`, which is exactly what makes it a handoff net
rather than a fixed pipeline.

Run: PYTHONPATH=. python3 examples/handoff.py
"""

import asyncio

from src import Agent, Workflow, append, go
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm


# Each specialist owns the tools for its own domain — a hard capability and
# permission boundary, not a prompt asking it to "please stay in your lane".
async def query_invoice(ctx=None):
    """Look up the order's billing facts."""
    return "O-1234: charged ¥399, not shipped"


async def query_blacklist(ctx=None):
    """Check the account against the fraud blacklist."""
    return "account not on any blacklist"


_VERDICT_HINT = (
    "After your analysis, end with exactly one of:\n"
    "  NEXT:<node> :: summary   if another specialist must act, or\n"
    "  NEXT:close :: decision   if you can settle the case."
)


def specialist(name, script, tools=()):
    return Agent(
        name,
        model=env_llm(ScriptedLlm(list(script))),
        instruction=f"You are the {name} specialist. Ground your answer in your "
        f"tools, then decide the next holder. {_VERDICT_HINT}",
        tools=list(tools),
    )


def parse_verdict(text: str) -> tuple[str, str]:
    """'NEXT:risk :: summary' -> ('risk', 'summary'); the runtime routing rule."""
    head, sep, rest = text.partition("::")
    if not sep or "NEXT:" not in head:
        # Fail loud at the boundary: a real model that ignored the verdict
        # format should report "bad verdict", not the misleading "Goto target
        # does not exist" you would get from routing the raw sentence.
        raise ValueError(
            "specialist verdict must end with 'NEXT:<node> :: summary' "
            f"(or 'NEXT:close :: decision'); got: {text!r}"
        )
    target = head.replace("NEXT:", "").strip().lower()
    if not target:
        raise ValueError(f"empty NEXT target in verdict: {text!r}")
    return target, rest.strip()


def specialist_node(agent: Agent):
    """Run one self-contained specialist, then hand off to whichever peer its
    verdict names — the node never decides routing itself, the specialist's
    conclusion does (with a real model this is a model-made routing decision).
    The node takes its graph name from agent.name, so it can never be wired to
    a mismatched specialist."""

    async def body(incoming, ctx):
        brief = incoming if isinstance(incoming, str) else str(incoming or "")
        verdict = await agent.delegate(brief)  # verdict format lives in the system instruction
        nxt, summary = parse_verdict(verdict)
        return go(nxt, summary, trail=[f"{agent.name} -> {nxt}"])

    return body


def _close(decision, ctx):  # terminal: whoever holds the case at the end settles it
    return decision


# ---- shape 1: a fixed handoff chain -----------------------------------------
def build_chain() -> Workflow:
    triage = specialist(
        "triage",
        [
            "NEXT:billing :: Refund request for O-1234; billing facts are needed before anything else."
        ],
    )
    billing = specialist(
        "billing",
        [
            ToolCall("query_invoice", {}),
            "NEXT:risk :: Charged ¥399 and not shipped; risk must confirm there is no fraud.",
        ],
        tools=[query_invoice],
    )
    risk = specialist(
        "risk",
        [
            ToolCall("query_blacklist", {}),
            "NEXT:close :: No fraud record; approve the ¥399 refund.",
        ],
        tools=[query_blacklist],
    )

    wf = Workflow()
    wf.channel("trail", append())
    wf.add_node("triage", specialist_node(triage))
    wf.add_node("billing", specialist_node(billing))
    wf.add_node("risk", specialist_node(risk))
    wf.add_node("close", _close, terminal=True)
    # Deliberately NO edges between specialists: each activation after triage
    # is a runtime `go`. Only the entry and the registered peers are declared.
    wf.entry("triage")
    return wf


# ---- shape 2: a swarm net that can hand a case back -------------------------
def build_swarm() -> Workflow:
    # billing is entered twice (risk hands the case back once), so its script
    # covers two independent runs: investigate, then reconcile the discrepancy.
    billing = specialist(
        "billing",
        [
            ToolCall("query_invoice", {}),
            "NEXT:risk :: Charged ¥399 and not shipped; check fraud before refunding.",
            "NEXT:risk :: Reconciled: ¥399 is correct — ¥499 was the list price before a ¥100 coupon. Safe to refund ¥399.",
        ],
        tools=[query_invoice],
    )
    # risk likewise runs twice: first it bounces the amount mismatch back to
    # billing, then, once reconciled, it settles.
    risk = specialist(
        "risk",
        [
            ToolCall("query_blacklist", {}),
            "NEXT:billing :: Blacklist is clean, but the customer claims ¥499 while the invoice says ¥399 — billing must reconcile before I decide.",
            "NEXT:close :: Amount reconciled and no fraud: approve the ¥399 refund.",
        ],
        tools=[query_blacklist],
    )

    wf = Workflow()
    wf.channel("trail", append())
    wf.add_node("billing", specialist_node(billing))
    wf.add_node("risk", specialist_node(risk))
    wf.add_node("close", _close, terminal=True)
    wf.entry("billing")  # no triage, no supervisor — peers hand off among themselves
    return wf


async def main():
    task = "Customer was charged but received nothing; requesting a refund for O-1234."

    print("=== shape 1: handoff chain (control never returns) ===")
    r = await build_chain().run(task)
    print("settled:", r.output)
    print("control trail:", "  ".join(r.state["trail"]))
    print(
        "attempts per specialist:",
        {n: r.run.state_of(n).attempts for n in ("triage", "billing", "risk")},
    )
    print(f"waves: {r.metrics['waves']}")

    print("\n=== shape 2: swarm net (peers can hand the case back) ===")
    s = await build_swarm().run(task)
    print("settled:", s.output)
    print("control trail:", "  ".join(s.state["trail"]))
    print(
        "attempts:",
        {n: s.run.state_of(n).attempts for n in ("billing", "risk")},
        "— the case bounced back once before settling",
    )
    print("nodes on the graph:", sorted(n for n in s.run.plan.nodes), "(no supervisor)")
    print(f"waves: {s.metrics['waves']}")


if __name__ == "__main__":
    asyncio.run(main())
