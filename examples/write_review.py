"""Write, review, revise — a generator and a critic agent iterate until
the draft passes.

Three agents on one graph: the writer drafts, the critic reviews, and a
judge routes by content — a failing review carries the criticism back to a
reviser (the back edge of the loop), a passing one goes straight to
finalize. Whoever arrives at finalize supplies the output, hence
join="any": the two routes are mutually exclusive, exactly one runs.

No new mechanism: two agents, a conditional branch, and one back edge —
quality iteration is a loop like any other, with agents in the nodes.

Run: PYTHONPATH=. python3 examples/write_review.py
"""

import asyncio

from src import Agent, Workflow, go
from src.runtime.llm import ScriptedLlm, env_llm


async def main():
    def author(name, line):
        # Scripted so the demo runs offline; swap in a real model when ready.
        return Agent(name, model=env_llm(ScriptedLlm([line])), instruction=f"You are the {name}.")

    writer = author("writer", "Draft: revenue grew this quarter; recommend expanding.")
    critic = author("critic", "Review: lacks data sources — revise before finalizing.")
    reviser = author(
        "reviser", "Revision: added the source for +18% YoY revenue; conclusion unchanged."
    )

    async def judge(review, ctx):
        # Route by content: a failing review goes to revision, a passing one
        # straight to finalize — the runtime picks the side.
        target = "revise" if "lacks" in str(review).lower() else "finalize"
        return go(target, review, verdict=target, review=review)

    async def finalize(text, ctx):
        # Input here: the revised draft when it went through revision, or the
        # critic's review when finalized directly.
        return f"Finalized: {text}"

    wf = Workflow()
    wf.add_node("writer", writer)
    wf.add_node("critic", critic)
    wf.add_node("judge", judge)
    wf.add_node("revise", reviser)
    # Convergence point: either judge finalizes directly or revise does after
    # rewriting — exactly one of the two arrives.
    wf.add_node("finalize", finalize, join="any", terminal=True)

    wf.add_edge("writer", "critic")
    wf.add_edge("critic", "judge")
    wf.add_edge("revise", "finalize")
    wf.branch(
        "judge", {"revise": "revise", "finalize": "finalize"}, decide=lambda s: s.get("verdict")
    )
    wf.entry("writer")

    result = await wf.run("Write a quarterly business summary")
    print(result.output)
    print(f"\nwaves: {result.metrics['waves']} — draft, review, then one revision loop")


if __name__ == "__main__":
    asyncio.run(main())
