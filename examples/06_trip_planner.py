"""06 Trip planning — the main flow fans out three specialist sub-agents in
one shot, then converges into one itinerary.

The three sub-agents (itinerary, dining, traffic) are parallel Workflow
nodes: concurrent in one wave, each with its own model; the synth node waits
for all three (join="all") and then assembles. This is the call semantics:
dispatched out, results all come back.

Run: PYTHONPATH=. python3 examples/06_trip_planner.py
"""

import asyncio

from src import Agent, Workflow
from src.runtime.llm import ScriptedLlm, env_llm


def specialist(name, line):
    # Each specialist is played by a fixed script so the demo runs offline;
    # swap in a real model when ready.
    return Agent(name, model=env_llm(ScriptedLlm([line])), instruction=f"You handle {name}.")


async def main():
    itinerary = specialist("itinerary", "Day 1 the Bund, Day 2 Disneyland")
    dining = specialist("dining", "local-cuisine dinner reserved")
    traffic = specialist("traffic", "Metro Line 2 connection, taxi as backup")

    wf = Workflow()
    wf.add("itinerary", itinerary)
    wf.add("dining", dining)
    wf.add("traffic", traffic)

    async def synth(parts, ctx):
        return "Itinerary ready:\n- " + "\n- ".join(parts.values())

    wf.add("synth", synth, join="all", terminal=True)

    wf.entry("itinerary", "dining", "traffic")  # the three sub-agents run in parallel in one wave
    wf.edge("itinerary", "synth")
    wf.edge("dining", "synth")
    wf.edge("traffic", "synth")

    result = await wf.run("A two-day trip to Shanghai")
    print(result.output)
    print(f"waves: {result.metrics['waves']} (the three sub-agents ran in one wave)")


if __name__ == "__main__":
    asyncio.run(main())
