"""Write, review, revise once — a generator, a critic, and a rework branch.

Three agents on one graph: the writer drafts, the critic reviews, and a judge
routes by content. A failing review carries the criticism to a reviser (one
rework pass, then finalize), a passing one goes straight to finalize; whoever
arrives supplies the output (join="any") — the two routes are mutually
exclusive. The draft rides in shared state, so a passing review finalizes the
draft itself, not the review text. No new mechanism and no loop: agents in the
nodes, one conditional branch, a straight DAG.

``build(lang)`` is the single assembly point (bilingual); ``main`` runs English.
Run: PYTHONPATH=. python3 examples/write_review.py
"""

import asyncio

from src import Agent, Outcome, Workflow, go
from src.runtime.llm import ScriptedLlm, env_llm


def build(lang: str = "en") -> Workflow:
    t = {
        "en": {
            "instruction_fmt": "You are {name}.",
            "writer": "Draft: revenue grew this quarter; recommend expanding.",
            "critic": "Review: lacks data sources — revise before finalizing.",
            "reviser": "Revision: added the source for +18% YoY revenue; conclusion unchanged.",
            "fail_kw": "lacks",
            "finalize_fmt": "Finalized: {text}",
        },
        "zh": {
            "instruction_fmt": "你是{name}",
            "writer": "初稿：本季度营收增长，建议扩张。",
            "critic": "审阅意见：缺少数据来源，需要补充后再定稿。",
            "reviser": "修订稿：补充营收同比 +18% 的来源，结论不变。",
            "fail_kw": "补充",
            "finalize_fmt": "定稿完成：{text}",
        },
    }[lang]
    wf = Workflow()

    def author(name, line):
        return Agent(
            name,
            model=env_llm(ScriptedLlm([line])),
            instruction=t["instruction_fmt"].format(name=name),
        )

    writer = author("writer", t["writer"])
    critic = author("critic", t["critic"])
    reviser = author("reviser", t["reviser"])

    async def keep(text, ctx):
        # The draft into shared state: the judge only receives the review along
        # its edge, so without this a passing review would finalize the review
        # text instead of the draft.
        return Outcome.ok(text, draft=text)

    async def judge(review, ctx):
        target = "revise" if t["fail_kw"] in str(review).lower() else "finalize"
        payload = review if target == "revise" else ctx.shared["draft"]
        return go(target, payload, verdict=target)

    async def finalize(text, ctx):
        return t["finalize_fmt"].format(text=text)

    wf.add_node("writer", writer)
    wf.add_node("keep", keep)
    wf.add_node("critic", critic)
    wf.add_node("judge", judge)
    wf.add_node("revise", reviser)
    wf.add_node("finalize", finalize, join="any", terminal=True)
    wf.add_edge("writer", "keep")
    wf.add_edge("keep", "critic")
    wf.add_edge("critic", "judge")
    wf.add_edge("revise", "finalize")
    wf.branch(
        "judge", {"revise": "revise", "finalize": "finalize"}, decide=lambda s: s.get("verdict")
    )
    wf.entry("writer")
    return wf


async def main():
    r = await build("en").run("Write a quarterly business summary")
    print(r.output)
    print(f"\nwaves: {r.metrics['waves']} — draft, review, one rework branch, finalize")


if __name__ == "__main__":
    asyncio.run(main())
