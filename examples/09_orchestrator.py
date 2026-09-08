"""09 Service audit, orchestrator-worker — how many workers? The graph doesn't
know; the planner reads the live catalog and the runtime stamps out copies.

The graph has ONE reviewer node. How many times it runs is decided at runtime:
the planner agent calls the catalog tool, writes a numbered plan ("1. audit
auth / 2. audit payment / ..."), and dispatch turns each line into a Send —
one copy of the reviewer template per service, all running concurrently in
one wave. synth joins every copy (join="all") and writes the summary.

A static graph fans out a fixed shape; here the fan-out width is data. Add a
fifth service to the catalog tomorrow and the same graph runs five reviewers
without touching a line.

Run: PYTHONPATH=. python3 examples/09_orchestrator.py
"""

import asyncio

from src import Agent, Workflow, send
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm
from src.runtime.plan_first import parse_numbered_list

# What a check finds per service — standing in for each service's health API.
FINDINGS = {
    "auth": "p95 41ms, errors 0.0% — pass",
    "payment": "p95 188ms, errors 0.3% — pass, watch item",
    "search": "p95 320ms, errors 2.1% — FAIL: retry storm from a cold cache",
    "notification": "p95 65ms, errors 0.1% — pass",
}


async def main():
    # The planner's only tool: what actually exists *right now* — the fan-out
    # width comes from here, not from the graph.
    async def catalog(ctx=None):
        """List the services deployed in this environment."""
        return "deployed services: auth, payment, search, notification"

    planner = Agent(
        "planner",
        model=env_llm(
            ScriptedLlm(
                [
                    ToolCall("catalog", {}),
                    "1. audit auth\n2. audit payment\n3. audit search\n4. audit notification",
                ]
            )
        ),
        instruction=(
            "You plan a service audit: call the catalog, then output one "
            "numbered line per service, 'N. audit <service>'."
        ),
        tools=[catalog],
    )

    audited: dict[str, str] = {}  # filled by whichever copy runs — display only

    async def dispatch(plan_text, ctx):
        # Each plan line becomes one Send: a fresh copy of the reviewer
        # template, keyed so results map back to their service.
        steps = parse_numbered_list(plan_text)
        return [send("reviewer", step, key=step["id"]) for step in steps]

    async def reviewer(step, ctx):
        service = step["instruction"].removeprefix("audit ")
        finding = FINDINGS[service]
        audited[service] = finding
        return f"{service}: {finding}"

    synth = Agent(
        "synth",
        model=env_llm(
            ScriptedLlm(
                [
                    "3 of 4 services pass; search fails on a retry storm — "
                    "roll back the cache change before the release."
                ]
            )
        ),
        instruction="You write the audit summary in two sentences.",
    )

    wf = Workflow()
    wf.add("planner", planner)
    wf.add("dispatch", dispatch)
    wf.add("reviewer", reviewer, template=True)  # copies stamped at runtime
    wf.add("synth", synth, terminal=True)  # join="all": waits for every copy
    wf.edge("planner", "dispatch")
    wf.edge("reviewer", "synth")
    wf.entry("planner")

    result = await wf.run("Audit every deployed service before the release")
    print("Summary:", result.output)
    print()
    print("One reviewer node on the graph; these copies ran in one wave:")
    for service in sorted(audited):
        print(f"  {service}: {audited[service]}")
    print(f"\nwaves: {result.metrics['waves']} (plan, dispatch, review, synthesize)")


if __name__ == "__main__":
    asyncio.run(main())
