"""Refund routing as a handoff chain and a swarm net — control passes between
specialists and never comes back; there is no supervisor in the flow.

What this file shows (two shapes of *transfer*, the counterpart of *call*):
- ``build_chain(lang)`` — triage -> billing -> risk -> close, a fixed order.
  Each specialist works the case with its OWN model and tools, then hands a
  one-line summary (the go payload) to the next; after a handoff the previous
  node never runs again (attempts stay 1) and only the summary travels.
- ``build_swarm(lang)`` — no triage, no supervisor: billing and risk may hand
  the case BACK to each other when the facts do not line up (a back-edge is
  just another `go` to a registered node); whoever can settle it goes to close.
  The next holder is chosen from the specialist's own verdict at runtime
  ("NEXT:.."), not from edges drawn up front. Scripts are finite and the
  kernel's max_waves backs them up, so the net always reaches a verdict.
- ``build(lang)`` is the playground entry point (the swarm net).

Delegation (see after_sales.py) goes and comes back; a handoff goes and does
not come back: the current node hands a summary to another Agent node with
`go` and draws no return edge, so control leaves for good. Who runs next is
entirely a runtime `go`, which is what makes this a handoff net rather than a
fixed pipeline; the static in-edges are GATED (live only once the case was
handed to that node), so a peer the case never reached is swept as a dead
branch and an early settle terminates cleanly instead of stalling.

Run: PYTHONPATH=. python examples/handoff.py
"""

import asyncio
import re

from src import Agent, Workflow, append, go, merge
from src.kernel import ToolCall
from src.runtime.agent import spawn_agent
from src.runtime.llm import ScriptedLlm, env_llm


# The verdict marker "NEXT:<node> ::" is language-neutral protocol; only the
# summary after "::" is localized, so the same parse_verdict works in both.
def _texts(lang: str) -> dict:
    return {
        "en": {
            "invoice": "O-1234: charged ¥399, not shipped",
            "blacklist": "account not on any blacklist",
            "role_fmt": "You are the {name} specialist. Ground your answer in your "
            "tools, then decide the next holder. {hint}",
            "hint": "Valid next holders: {peers}. After your analysis, end with "
            "exactly one line 'NEXT:<holder> :: <summary>' "
            "(e.g. NEXT:risk :: Charged ¥399, not shipped; fraud check needed) "
            "and write nothing after that line.",
            "chain": {
                "triage": [
                    "NEXT:billing :: Refund request for O-1234; billing facts are needed before anything else."
                ],
                "billing": [
                    ToolCall("query_invoice", {}),
                    "NEXT:risk :: Charged ¥399 and not shipped; risk must confirm there is no fraud.",
                ],
                "risk": [
                    ToolCall("query_blacklist", {}),
                    "NEXT:close :: No fraud record; approve the ¥399 refund.",
                ],
            },
            "swarm": {
                "billing": [
                    ToolCall("query_invoice", {}),
                    "NEXT:risk :: Charged ¥399 and not shipped; check fraud before refunding.",
                    "NEXT:risk :: Reconciled: ¥399 is correct — ¥499 was the list price before a ¥100 coupon. Safe to refund ¥399.",
                ],
                "risk": [
                    ToolCall("query_blacklist", {}),
                    "NEXT:billing :: Blacklist is clean, but the customer claims ¥499 while the invoice says ¥399 — billing must reconcile before I decide.",
                    "NEXT:close :: Amount reconciled and no fraud: approve the ¥399 refund.",
                ],
            },
        },
        "zh": {
            "invoice": "O-1234：已扣款 ¥399，未发货",
            "blacklist": "账户不在任何黑名单上",
            "role_fmt": "你是{name}专员，先基于工具核实，再决定下一个持有者。{hint}",
            "hint": "合法的下一棒：{peers}。分析完后，回复必须以且仅以一行"
            "'NEXT:<持有者> :: <摘要>'结尾"
            "（例：NEXT:risk :: 已扣款 ¥399 且未发货，需风控确认），"
            "这一行之后不要再写任何内容。",
            "chain": {
                "triage": ["NEXT:billing :: O-1234 的退款请求，需要先查账单事实。"],
                "billing": [
                    ToolCall("query_invoice", {}),
                    "NEXT:risk :: 已扣款 ¥399 且未发货，需风控确认无欺诈。",
                ],
                "risk": [
                    ToolCall("query_blacklist", {}),
                    "NEXT:close :: 无欺诈记录，批准退 ¥399。",
                ],
            },
            "swarm": {
                "billing": [
                    ToolCall("query_invoice", {}),
                    "NEXT:risk :: 已扣款 ¥399 且未发货，退款前先查欺诈。",
                    "NEXT:risk :: 已核对：¥399 正确——¥499 是 ¥100 优惠券前的标价，可安全退 ¥399。",
                ],
                "risk": [
                    ToolCall("query_blacklist", {}),
                    "NEXT:billing :: 黑名单干净，但客户称 ¥499 而发票是 ¥399，我决定前需账单核对。",
                    "NEXT:close :: 金额已核对且无欺诈：批准退 ¥399。",
                ],
            },
        },
    }[lang]


def _domain_tools(t: dict):
    async def query_invoice(ctx=None):
        """Look up the order's billing facts."""
        return t["invoice"]

    async def query_blacklist(ctx=None):
        """Check the account against the fraud blacklist."""
        return t["blacklist"]

    return query_invoice, query_blacklist


def specialist(name: str, script: list, *, t: dict, tools=(), peers=()) -> Agent:
    hint = t["hint"].format(peers=", ".join(peers) if peers else "close")
    return Agent(
        name,
        model=env_llm(ScriptedLlm(list(script))),
        instruction=t["role_fmt"].format(name=name, hint=hint),
        tools=list(tools),
    )


_VALID_TARGET = re.compile(r"^[a-z0-9_-]+$")


def parse_verdict(text: str) -> tuple[str, str]:
    """Take the LAST 'NEXT:<node> :: summary' in the reply -> (node, summary).

    A real model often surrounds its verdict with prose — a whole triage note
    before it, sometimes a second NEXT after it; only the final NEXT line is
    the routing fact, so rpartition anchors there. The target must then be a
    bare identifier — anything else fails loudly HERE as "bad verdict", never
    as a misleading "Goto target does not exist" from routing the raw prose."""
    _, sep, tail = text.rpartition("NEXT:")
    if not sep:
        raise ValueError(
            "specialist verdict must end with 'NEXT:<node> :: summary' "
            f"(or 'NEXT:close :: decision'); got: {text!r}"
        )
    target, sep2, summary = tail.partition("::")
    target = target.strip().lower()
    if not sep2 or not _VALID_TARGET.match(target):
        raise ValueError(
            f"bad verdict target {target!r}: expected '<node> :: <summary>' "
            "with a bare registered node name"
        )
    return target, summary.strip()


def _handed(node: str):
    """Edge gate: live only once the case was actually handed to ``node``.
    Combined with the sweep, this is what lets the net settle early — a peer
    the case never reached is a dead branch, skipped, not a stall."""

    def live(shared) -> bool:
        return bool(shared.get("handed", {}).get(node))

    return live


def specialist_node(agent: Agent):
    """Run one self-contained specialist, then hand off to whichever peer its
    verdict names — the node never decides routing itself, the specialist's
    conclusion does (with a real model this is a model-made routing decision).
    The node takes its graph name from agent.name, so it can never be wired to
    a mismatched specialist."""

    async def body(incoming, ctx):
        brief = incoming if isinstance(incoming, str) else str(incoming or "")
        # run the specialist on this host scheduler (shared ledger); its verdict
        # (a model-made routing decision) names the next holder.
        result = await spawn_agent(ctx, agent, brief)
        try:
            nxt, summary = parse_verdict(result.get("output"))
        except ValueError as exc:
            # The boundary refuses to guess; one repair round feeds the format
            # error back and re-runs the specialist — the same law as a tool
            # failure: feedback to the model, not a dead run. The retry is a
            # SECOND child Run for this agent, deliberate and visible in trace.
            # (exc carries the previous reply, so the model can just append a
            # compliant verdict line; a second failure propagates unchanged.)
            result = await spawn_agent(
                ctx,
                agent,
                f"{brief}\n\n[format error] {exc}\n"
                "Reply again and end with 'NEXT:<holder> :: <summary>'.",
            )
            nxt, summary = parse_verdict(result.get("output"))
        # go with no edge back = hand off and never return. The `handed` fact
        # is the other half of the gated in-edge: it marks WHO actually
        # received the case, so whoever never did can be swept, not stranded.
        return go(nxt, summary, trail=[f"{agent.name} -> {nxt}"], handed={nxt: True})

    return body


def _close(decision, ctx):  # terminal: whoever holds the case at the end settles it
    return decision


# ---- shape 1: a fixed handoff chain -----------------------------------------
def build_chain(lang: str = "en") -> Workflow:
    t = _texts(lang)
    query_invoice, query_blacklist = _domain_tools(t)
    # The chain is a FIXED order, so each specialist's valid next holder is
    # exactly the one the order names — the model picks, but within one hop.
    triage = specialist("triage", t["chain"]["triage"], t=t, peers=("billing",))
    billing = specialist("billing", t["chain"]["billing"], t=t, tools=[query_invoice], peers=("risk",))
    risk = specialist("risk", t["chain"]["risk"], t=t, tools=[query_blacklist], peers=("close",))

    wf = Workflow()
    wf.channel("trail", append())
    wf.channel("handed", merge())
    wf.add_node("triage", specialist_node(triage))
    wf.add_node("billing", specialist_node(billing))
    wf.add_node("risk", specialist_node(risk))
    wf.add_node("close", _close, terminal=True)
    # Gated in-edges: a specialist's edge is live only once the case was handed
    # to it. Activation is still the runtime `go` (a handoff chain, not a
    # pipeline); the gate only lets the sweep skip a never-handed specialist
    # if a real model settles early at close.
    wf.add_edge("triage", "billing", when=_handed("billing"))
    wf.add_edge("billing", "risk", when=_handed("risk"))
    wf.entry("triage")
    return wf


# ---- shape 2: a swarm net that can hand a case back -------------------------
def build_swarm(lang: str = "en") -> Workflow:
    t = _texts(lang)
    query_invoice, query_blacklist = _domain_tools(t)
    # billing is entered twice (risk hands the case back once), so its script
    # covers two independent runs: investigate, then reconcile the discrepancy.
    billing = specialist(
        "billing", t["swarm"]["billing"], t=t, tools=[query_invoice], peers=("risk", "close")
    )
    # risk likewise runs twice: first it bounces the amount mismatch back to
    # billing, then, once reconciled, it settles.
    risk = specialist(
        "risk", t["swarm"]["risk"], t=t, tools=[query_blacklist], peers=("billing", "close")
    )

    wf = Workflow()
    wf.channel("trail", append())
    wf.channel("handed", merge())
    wf.add_node("billing", specialist_node(billing))
    wf.add_node("risk", specialist_node(risk))
    wf.add_node("close", _close, terminal=True)
    # Same gate as the chain: whoever the case never reached is a dead branch
    # once someone settles, so an early NEXT:close terminates cleanly. Only
    # risk needs one — billing is the entry, and entry nodes always run and
    # are never swept, so an in-edge gate on it would be dead wiring.
    wf.add_edge("billing", "risk", when=_handed("risk"))
    wf.entry("billing")  # no triage, no supervisor — peers hand off among themselves
    return wf


def build(lang: str = "en") -> Workflow:
    """Playground entry: the swarm net (peers can hand the case back)."""
    return build_swarm(lang)


async def main():
    task = "Customer was charged but received nothing; requesting a refund for O-1234."

    print("=== shape 1: handoff chain (control never returns) ===")
    r = await build_chain("en").run(task)
    print("settled:", r.output)
    print("control trail:", "  ".join(r.state["trail"]))
    print(
        "attempts per specialist:",
        {n: r.run.state_of(n).attempts for n in ("triage", "billing", "risk")},
    )
    print(f"waves: {r.metrics['waves']}")

    print("\n=== shape 2: swarm net (peers can hand the case back) ===")
    s = await build_swarm("en").run(task)
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
