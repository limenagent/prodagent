"""10 Proposal review, blackboard — nobody calls anybody; every expert only
writes to a shared board, and a moderator reads it to decide.

Three experts (finance / legal / ops) review a rollout proposal in parallel,
each appending its opinion to one shared append channel — opinions accumulate,
nobody overwrites anybody. The moderator joins all of this round's opinions
(join="all"), reads the whole board, and decides by content: not converged,
Goto back to the fanout, which re-arms the experts and itself for another
round; converged, straight to final. Round 2's experts see the objections on
the board and revise — their scripted models move to their next line, the
same agent object re-run.

Two backstops end the debate even without consensus: the moderator's round
cap (debate must terminate in a verdict), and the kernel's max_waves behind
it. With a real model behind env_llm the experts may never say the magic
word — the round cap is what keeps this runnable, not luck.

Run: PYTHONPATH=. python3 examples/10_blackboard.py
"""

import asyncio

from src import Agent, Workflow, append, go, last
from src.kernel import Goto, Outcome
from src.runtime.llm import ScriptedLlm, env_llm

EXPERTS = ("finance", "legal", "ops")
MAX_ROUNDS = 3  # the debate must end in a verdict even without consensus


async def main():
    # One script line per round: round 1 objects, round 2 — having read the
    # objections on the board — agrees. The cursor advances on each re-run.
    scripts = {
        "finance": [
            "Objection: the budget doubles this quarter's cap — needs a phased rollout.",
            "Agreed: phased rollout keeps spend inside this quarter's cap.",
        ],
        "legal": [
            "Objection: the EU data-processing clause is missing from the contract.",
            "Agreed: the updated contract adds the EU data-processing clause.",
        ],
        "ops": [
            "Concern: no maintenance window is scheduled for the rollout.",
            "Agreed: the Sunday 02:00 window works for operations.",
        ],
    }

    wf = Workflow()
    wf.channel("board", append())  # opinions accumulate across rounds
    wf.channel("round", last(0))

    def expert_node(name):
        agent = Agent(
            name,
            model=env_llm(ScriptedLlm(list(scripts[name]))),
            instruction=f"You are the {name} reviewer; state your position on the proposal.",
        )

        async def run(_, ctx):
            # The expert thinks on its own; only its verdict lands on the board.
            # Earlier opinions travel with the task, so a real model can
            # actually respond to them (the scripted one plays the arc).
            digest = " | ".join(f"{op['by']}: {op['view']}" for op in ctx.shared["board"])
            task = f"round {ctx.shared['round'] + 1}: review the proposal"
            if digest:
                task += f". Board so far: {digest}"
            view = await agent.delegate(task)
            return {"board": [{"by": name, "round": ctx.shared["round"], "view": view}]}

        return run

    async def fanout(_, ctx):
        # Re-arm the experts and the moderator; the edges still decide that
        # experts run in parallel first and the moderator waits for them all.
        return Outcome(control=[Goto.rejoin(n) for n in (*EXPERTS, "moderate")])

    async def moderate(_, ctx):
        board, rnd = ctx.shared["board"], ctx.shared["round"]
        this_round = [op for op in board if op["round"] == rnd]
        converged = bool(this_round) and all(op["view"].startswith("Agreed") for op in this_round)
        # Round cap: the debate ends in a verdict even if consensus never forms.
        if converged or rnd + 1 >= MAX_ROUNDS:
            verdict = (
                "Consensus: proceed with the phased rollout."
                if converged
                else "Round cap reached without full consensus; proceeding with the phased rollout."
            )
            return go("final", verdict)
        return go("fanout", round=rnd + 1)  # another round, objections stay on the board

    async def final(verdict, ctx):
        return f"{verdict} ({len(ctx.shared['board'])} opinions were written along the way.)"

    for name in EXPERTS:
        wf.add(name, expert_node(name))
        wf.edge("fanout", name)
        wf.edge(name, "moderate")
    wf.add("fanout", fanout)
    wf.add("moderate", moderate, join="all")  # every expert of this round, every round
    wf.add("final", final, terminal=True)
    wf.entry("fanout")

    result = await wf.run("Should we roll out the new pricing engine next week?")
    print(result.output)
    print()
    print("The board, as it accumulated (nothing was ever overwritten):")
    for op in result.state["board"]:
        print(f"  round {op['round'] + 1} | {op['by']:>8}: {op['view']}")
    print(f"\nwaves: {result.metrics['waves']} — two full rounds plus the verdict")


if __name__ == "__main__":
    asyncio.run(main())
