"""Compliance audit — parallel checks + human approval on the write; a
denial changes only the action, not the whole rerun.

Two audit branches run in parallel and converge into a conclusion; "freeze
accounts" truly stops and waits for a human (wait_human). If denied, the flow
does not re-run the checks — it takes the other edge to the report,
annotated "not approved".

Run: PYTHONPATH=. python3 examples/compliance_audit.py
"""

import asyncio

from src import Workflow, go, wait_human


def build_audit_workflow():
    wf = Workflow()

    async def screen_suspicious(x, ctx):
        return {"flags": "fast-in fast-out transfers detected"}

    async def screen_accounts(x, ctx):
        return {"links": "linked to 3 accounts of the same origin"}

    async def synthesize(x, ctx):
        s = ctx.shared
        return f"Synthesis: {s['flags']}; {s['links']}. Recommend freezing."

    async def freeze(summary, ctx):
        if ctx.resume_value is None:
            # First time here: nobody has been asked yet — suspend, handing
            # out the information to confirm along the way.
            return wait_human("Freeze these accounts?", {"accounts": ["A1", "A2"]})
        if not ctx.resume_value.get("approved"):
            return go("report", summary, decision="freeze recommended but not approved this time")
        return go("report", summary, decision="froze A1 & A2")

    async def report(summary, ctx):
        return f"{summary} | action taken: {ctx.shared['decision']}"

    wf.add_node("screen_suspicious", screen_suspicious)
    wf.add_node("screen_accounts", screen_accounts)
    wf.add_node("synthesize", synthesize, join="all")
    wf.add_node("freeze", freeze)
    wf.add_node("report", report, terminal=True)
    wf.entry("screen_suspicious", "screen_accounts")  # two entries run in parallel in one wave
    wf.add_edge("screen_suspicious", "synthesize")
    wf.add_edge("screen_accounts", "synthesize")  # join="all": converge only when both arrive
    wf.add_edge("synthesize", "freeze")
    wf.add_edge("freeze", "report")
    return wf


async def main():
    wf = build_audit_workflow()

    first = await wf.run("Audit account A1")
    print("First run status:", first.status, "— suspended awaiting approval")

    # The human reads the material and picks "deny the freeze"; the run
    # resumes from its suspension — every earlier check result is still there.
    second = await wf.resume(first.run_id, {"approved": False})
    print("Final report after resume:", second.output)


if __name__ == "__main__":
    asyncio.run(main())
