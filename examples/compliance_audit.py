"""Compliance audit — parallel checks + human approval on the write.

Two audit branches run in parallel and converge (join="all") into a conclusion;
"freeze accounts" truly stops and waits for a person (wait_human). If denied,
the flow does not re-run the checks — it takes the other edge to the report,
annotated "not approved".

``build(lang)`` is the single assembly point (bilingual); ``main`` runs English
and denies the freeze to show that only the action changes. In the playground
you approve or deny yourself.

Run: PYTHONPATH=. python3 examples/compliance_audit.py
"""

import asyncio

from src import Workflow, go, wait_human


def build(lang: str = "en") -> Workflow:
    t = {
        "en": {
            "flags": "fast-in fast-out transfers detected",
            "links": "linked to 3 accounts of the same origin",
            "synth_fmt": "Synthesis: {flags}; {links}. Recommend freezing.",
            "question": "Freeze accounts A1 and A2?",
            "approved": "froze A1 & A2",
            "denied": "freeze recommended but not approved this time",
            "report_fmt": "{summary} | action taken: {decision}",
        },
        "zh": {
            "flags": "发现快进快出交易",
            "links": "关联到 3 个同源账户",
            "synth_fmt": "综合判断：{flags}；{links}，建议冻结。",
            "question": "批准冻结 A1、A2 两个账户吗？",
            "approved": "已冻结 A1、A2",
            "denied": "建议冻结，但本次未获批准",
            "report_fmt": "{summary}｜处置：{decision}",
        },
    }[lang]
    wf = Workflow()

    async def screen_suspicious(x, ctx):
        return {"flags": t["flags"]}

    async def screen_accounts(x, ctx):
        return {"links": t["links"]}

    async def synthesize(x, ctx):
        s = ctx.shared
        return t["synth_fmt"].format(flags=s["flags"], links=s["links"])

    async def freeze(summary, ctx):
        if ctx.resume_value is None:
            return wait_human(t["question"], {"accounts": ["A1", "A2"]})
        decision = t["approved"] if ctx.resume_value.get("approved") else t["denied"]
        return go("report", summary, decision=decision)

    async def report(summary, ctx):
        return t["report_fmt"].format(summary=summary, decision=ctx.shared["decision"])

    wf.add_node("screen_suspicious", screen_suspicious)
    wf.add_node("screen_accounts", screen_accounts)
    wf.add_node("synthesize", synthesize, join="all")
    wf.add_node("freeze", freeze)
    wf.add_node("report", report, terminal=True)
    wf.entry("screen_suspicious", "screen_accounts")  # two entries in parallel
    wf.add_edge("screen_suspicious", "synthesize")
    wf.add_edge("screen_accounts", "synthesize")
    wf.add_edge("synthesize", "freeze")
    wf.add_edge("freeze", "report")
    return wf


async def main():
    wf = build("en")
    first = await wf.run("Audit account A1")
    print("First run status:", first.status, "— suspended awaiting approval")
    second = await wf.resume(first.run_id, {"approved": False})  # deny the freeze
    print("Final report after resume:", second.output)


if __name__ == "__main__":
    asyncio.run(main())
