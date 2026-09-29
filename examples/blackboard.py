"""Proposal review — a blackboard where reviewers converge over rounds.

There is no supervisor routing messages. Each round, ``fanout`` re-arms the
expert nodes (a back-edge via Goto.rejoin); each expert runs on the shared
scheduler and writes only its verdict onto the board. ``moderate`` joins every
expert of the round: if all positions start with the agreement word the verdict
is consensus, otherwise another round is fanned out. A round cap guarantees a
verdict even if a real model never says the scripted word. The graph declares
the experts; who runs is a runtime rejoin — that is what makes it a net.

``build(lang)`` is the single assembly point (bilingual); ``main`` runs English.
Run: PYTHONPATH=. python -m examples.blackboard
"""

import asyncio

from src import Agent, Workflow, append, go, last
from src.kernel import Goto, Outcome
from src.runtime.agent import spawn_agent
from src.runtime.llm import ScriptedLlm, env_llm

EXPERTS = ("finance", "legal", "ops")
MAX_ROUNDS = 3


def build(lang: str = "en") -> Workflow:
    t = {
        "en": {
            "scripts": {
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
            },
            "agree": "Agreed",
            "verdict_ok": "Consensus: proceed with the phased rollout.",
            "verdict_cap": "Round cap reached without full consensus; proceeding with the phased rollout.",
            "final_note": "{n} opinions were written along the way.",
            "instruction_fmt": "You are the {name} reviewer; state your position on the proposal.",
        },
        "zh": {
            "scripts": {
                "finance": [
                    "反对：预算翻倍超出本季度上限——需要分期上线。",
                    "同意：分期上线后预算控制在本季度上限内。",
                ],
                "legal": [
                    "反对：合同缺少欧盟数据处理条款。",
                    "同意：更新后的合同已补充欧盟数据处理条款。",
                ],
                "ops": [
                    "顾虑：上线没有安排维护窗口。",
                    "同意：周日凌晨 2 点的窗口运维可接受。",
                ],
            },
            "agree": "同意",
            "verdict_ok": "达成共识：按分期方案上线。",
            "verdict_cap": "到达轮次上限仍未完全收敛，按分期方案上线。",
            "final_note": "板上先后留下了 {n} 条意见。",
            "instruction_fmt": "你是{name}评审，对方案给出你的立场。",
        },
    }[lang]
    wf = Workflow()
    wf.channel("board", append())
    wf.channel("round", last(0))

    def expert_node(name):
        agent = Agent(
            name,
            model=env_llm(ScriptedLlm(list(t["scripts"][name]))),
            instruction=t["instruction_fmt"].format(name=name),
        )

        async def run(_, ctx):
            digest = " | ".join(f"{op['by']}: {op['view']}" for op in ctx.shared["board"])
            task = f"round {ctx.shared['round'] + 1}: review the proposal"
            if digest:
                task += f". Board so far: {digest}"
            result = await spawn_agent(ctx, agent, task)
            return {"board": [{"by": name, "round": ctx.shared["round"], "view": result.get("output")}]}

        return run

    async def fanout(_, ctx):
        return Outcome(control=[Goto.rejoin(n) for n in (*EXPERTS, "moderate")])

    async def moderate(_, ctx):
        board, rnd = ctx.shared["board"], ctx.shared["round"]
        this_round = [op for op in board if op["round"] == rnd]
        converged = bool(this_round) and all(
            str(op["view"]).startswith(t["agree"]) for op in this_round
        )
        if converged or rnd + 1 >= MAX_ROUNDS:
            return go("final", t["verdict_ok"] if converged else t["verdict_cap"])
        return go("fanout", round=rnd + 1)

    async def final(text, ctx):
        note = t["final_note"].format(n=len(ctx.shared["board"]))
        return f"{text} ({note})"

    for name in EXPERTS:
        wf.add_node(name, expert_node(name))
        wf.add_edge("fanout", name)
        wf.add_edge(name, "moderate")
    wf.add_node("fanout", fanout)
    wf.add_node("moderate", moderate, join="all")
    wf.add_node("final", final, terminal=True)
    wf.entry("fanout")
    return wf


async def main():
    r = await build("en").run("Review the Q4 rollout proposal")
    print(r.output)
    print(f"\nwaves: {r.metrics['waves']}")


if __name__ == "__main__":
    asyncio.run(main())
