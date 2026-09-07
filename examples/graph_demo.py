"""The first example: no model attached at all — pure function nodes, so you
can see exactly how waves advance.

Run: PYTHONPATH=. python examples/graph_demo.py

The graph is a diamond:
        a
       / \\
      b   c   (b and c do not depend on each other; they run concurrently in one wave)
       \\ /
        d
Subscribe to the bus and you can watch which nodes the scheduler makes ready
and which complete in each wave — that is the BSP superstep.
"""

import asyncio

from src.kernel import (
    FnBody,
    Node,
    Outcome,
    Plan,
    Scheduler,
    add,
    append,
)


def build_plan() -> Plan:
    p = Plan(channels={"log": append(), "cost": add(0)})
    p.add(
        Node("a", FnBody(lambda x, ctx: Outcome.ok("from a", log=["a ran"], cost=1))),
        Node("b", FnBody(lambda x, ctx: Outcome.ok(f"b got: {x}", log=["b ran"], cost=2))),
        Node("c", FnBody(lambda x, ctx: Outcome.ok(f"c got: {x}", log=["c ran"], cost=3))),
        Node("d", FnBody(lambda x, ctx: Outcome.ok(x, log=["d summarized"])), terminal=True),
    )
    p.edge("a", "b")
    p.edge("a", "c")
    p.edge("b", "d")
    p.edge("c", "d")
    return p


async def main():
    plan = build_plan()
    sch = Scheduler()

    # Subscribe to the bus: observe every kernel step; a broken observer
    # cannot affect execution.
    sch.bus.on("node_started", lambda evt: print(f"  ▶ start {evt.data['node']}"))
    sch.bus.on("node_completed", lambda evt: print(f"  ✔ done {evt.data['node']}"))

    print("Running the diamond graph:")
    run = await sch.run(plan, task="seed input")
    print("Final result:", run.final_output)
    print("Shared state:", run.shared)
    print("Waves:", run.metrics["waves"], "(a | b,c | d — exactly three waves)")


if __name__ == "__main__":
    asyncio.run(main())
