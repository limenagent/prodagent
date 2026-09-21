"""Streaming and backpressure — nodes emit events while computing; a slow
consumer must not stall the kernel.

Inside a node, ctx.emit sends events out chunk by chunk (tokens, progress —
all the same). A subscription is a bounded queue; when it fills there are two
policies, and choosing between them is a trade-off you must make explicit:
- on_full="block": the producer waits at delivery, pushing the pressure back
  upstream (rather slow than lose);
- on_full="drop": the producer never waits; overflowing frames are dropped
  and counted (rather lose than stall).

This demo uses drop: the node emits 8 frames in one burst, the subscription
queue holds 2 and nobody drains it in time — only the earliest 2 frames
survive, the other 6 go on the dropped ledger, and the main flow is never
blocked for a moment.

Run: PYTHONPATH=. python3 examples/backpressure.py
"""

import asyncio

from src import Workflow
from src.kernel import Bus


async def main():
    bus = Bus()
    # Bounded subscription: capacity 2; on full, drop frames and count them —
    # never slow the producer down by waiting.
    sub = bus.subscribe("token", maxsize=2, on_full="drop")

    async def streamer(_, ctx):
        for i in range(8):
            await ctx.emit("token", i=i)  # emit while computing, like token-by-token output
        return "streaming done"

    wf = Workflow(bus=bus)
    wf.add_node("stream", streamer, terminal=True)
    wf.entry("stream")

    result = await wf.run("start")
    kept = []
    while not sub.queue.empty():
        kept.append((await sub.get())["i"])
    sub.close()

    print("Result:", result.output)
    print(f"frames kept by the queue: {kept} | frames dropped: {sub.dropped}")
    print(
        "With on_full='block', this would wait for the consumer to drain before "
        "continuing — the pressure goes back upstream."
    )


if __name__ == "__main__":
    asyncio.run(main())
