"""07 Incident response — parallel diagnosis sub-agents pin down the root
cause (call), then hand off to the repair agent (transfer).

Both multi-agent semantics appear in the same Workflow:
- call (delegation that returns): the diagnose node delegates two diagnosing
  agents in parallel, and both results come back;
- transfer (handoff that never returns): once the root cause is clear, the
  decide node goes to the repair agent node — no back edge is drawn on the
  graph; the repairer takes over with its own model and finishes the run,
  never returning to the diagnosis flow.

Run: PYTHONPATH=. python3 examples/07_aiops.py
"""

import asyncio

from src import Agent, Workflow, go
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm


async def main():
    # Read-only observability tools: the diagnosing agents have data to
    # check, instead of guessing from a vague "look at the CPU curve".
    async def cpu_metrics(ctx=None):
        """Read the last hour's CPU curve."""
        return "12:00 35% → 12:10 92% → 12:20 93% → 12:30 91% (spiking every ten minutes)"

    async def error_log(ctx=None):
        """Read the recent error log."""
        return "ERROR pool exhausted: connection wait timed out (5000ms), 37 times in the last hour"

    # With OPENAI_API_KEY set, a real model; otherwise the offline script
    # (the same switch the playground uses).
    def engineer(name, *script, tools=None):
        return Agent(
            name,
            model=env_llm(ScriptedLlm(list(script))),
            instruction=f"You are {name}: check the data with tools before concluding, in two sentences.",
            tools=tools or [],
        )

    diag_cpu = engineer(
        "diag_cpu",
        ToolCall("cpu_metrics", {}),
        "CPU saturates periodically every ten minutes; suspect queuing downstream.",
        tools=[cpu_metrics],
    )
    diag_log = engineer(
        "diag_log",
        ToolCall("error_log", {}),
        "Error log shows connection-wait timeouts; the pool is exhausted.",
        tools=[error_log],
    )
    repairer = engineer(
        "repairer", "Connection pool enlarged and upstream throttled; service recovered."
    )

    wf = Workflow()

    async def diagnose(x, ctx):
        # call: two diagnosing sub-agents in parallel; both results come back
        # and merge.
        cpu, log = await asyncio.gather(
            diag_cpu.delegate("check the CPU curve"), diag_log.delegate("check the error log")
        )
        root = f"root cause = pool exhaustion ({cpu}; {log})"
        return go("decide", root)

    async def decide(root, ctx):
        # transfer: go to the repair agent node; no back edge is drawn — once
        # handed over, it finishes there and never comes back.
        return go("repairer", root)

    wf.add("diagnose", diagnose)
    wf.add("decide", decide)
    wf.add("repairer", repairer, terminal=True)  # an Agent can be a graph node directly
    wf.edge("diagnose", "decide")
    wf.entry("diagnose")
    # Who you can hand to is exactly which Agent nodes were added to the
    # graph; repairer has no out-edge, so the handoff ends there.

    result = await wf.run("Order-service latency is spiking")
    print("Diagnosis and resolution:", result.output)


if __name__ == "__main__":
    asyncio.run(main())
