"""事件总线的完整背压：block 把压力传回生产端，drop 丢帧记账，关闭即注销。"""

import asyncio

import pytest

from src.kernel import Bus


async def test_block_subscription_pushes_back_producer():
    bus = Bus()
    sub = bus.subscribe("token", maxsize=1, on_full="block")
    await bus.fire("token", i=0)  # 第一帧入队，队列满
    # 没人取走时，第二帧的投递必须在生产端阻塞——这就是反压。
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.fire("token", i=1), timeout=0.1)
    item = await sub.get()  # 消费一帧腾出位置
    assert item["i"] == 0
    await asyncio.wait_for(bus.fire("token", i=2), timeout=0.1)  # 现在不再阻塞


async def test_drop_subscription_never_blocks_and_counts():
    bus = Bus()
    sub = bus.subscribe("token", maxsize=1, on_full="drop")
    for i in range(4):
        await asyncio.wait_for(bus.fire("token", i=i), timeout=0.1)
    assert sub.dropped == 3  # 只保住第一帧，其余丢帧记账
    assert (await sub.get())["i"] == 0


async def test_close_unregisters_subscription():
    bus = Bus()
    sub = bus.subscribe("x")
    assert sub in bus._subscriptions
    sub.close()
    assert sub not in bus._subscriptions  # 关闭即从总线摘除，不泄漏


async def test_node_can_stream_events_through_context():
    """节点执行中用 ctx.emit 逐块吐事件，订阅者按顺序收到（流式的最小闭环）。"""
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
