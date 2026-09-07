"""End-to-end bus backpressure: block propagates pressure to the producer, drop counts dropped frames, close unregisters."""

import asyncio

import pytest

from src.kernel import Bus


async def test_block_subscription_pushes_back_producer():
    bus = Bus()
    sub = bus.subscribe("token", maxsize=1, on_full="block")
    await bus.fire("token", i=0)  # first frame enqueued, queue now full
    # with nobody consuming, delivering the second frame must block the producer — that's backpressure.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.fire("token", i=1), timeout=0.1)
    item = await sub.get()  # consume one frame to free a slot
    assert item["i"] == 0
    await asyncio.wait_for(bus.fire("token", i=2), timeout=0.1)  # no longer blocks


async def test_drop_subscription_never_blocks_and_counts():
    bus = Bus()
    sub = bus.subscribe("token", maxsize=1, on_full="drop")
    for i in range(4):
        await asyncio.wait_for(bus.fire("token", i=i), timeout=0.1)
    assert sub.dropped == 3  # only the first frame survives, the rest are counted as dropped
    assert (await sub.get())["i"] == 0


async def test_close_unregisters_subscription():
    bus = Bus()
    sub = bus.subscribe("x")
    assert sub in bus._subscriptions
    sub.close()
    assert sub not in bus._subscriptions  # closing removes it from the bus, no leak


async def test_node_can_stream_events_through_context():
    """A node streams events chunk by chunk via ctx.emit, subscriber receives them in order (minimal streaming loop)."""
    from src import Workflow

    bus = Bus()
    received = []

    async def streamer(_, ctx):
        for piece in ("你", "好", "呀"):
            await ctx.emit("token", piece=piece)
        return "完成"

    wf = Workflow(bus=bus)
    wf.add("s", streamer, terminal=True)
    wf.entry("s")
    sub = bus.subscribe("token")
    r = await wf.run("")
    async for item in sub:
        received.append(item["piece"])
        if len(received) == 3:
            sub.close()
    assert r.output == "完成"
    assert received == ["你", "好", "呀"]
