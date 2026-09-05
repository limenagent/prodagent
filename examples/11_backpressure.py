"""11 流式与背压 —— 节点边算边吐事件，慢消费者不能拖垮内核。

节点执行中可以用 ctx.emit 逐块对外发事件（token、进度都一样）。订阅是一条有界
队列，队列满时有两种策略，这是一道必须明确的取舍：
- on_full="block"：生产端在投递处等待，把压力传回上游（宁可慢，也不丢）；
- on_full="drop"：生产端绝不等待，溢出的帧直接丢弃并计数（宁可丢，也不卡）。

这里用 drop 演示：节点一口气吐 8 帧、订阅队列只容 2 帧且没人及时取走，
于是只保住最早的 2 帧，其余 6 帧记在 dropped 账上，而主流程一刻也没被阻塞。

跑法：PYTHONPATH=. python3 examples/11_backpressure.py
"""

import asyncio

from src import Workflow
from src.kernel import Bus


async def main():
    bus = Bus()
    # 有界订阅：容量 2，满了就丢帧记账，绝不用等待去拖慢生产端。
    sub = bus.subscribe("token", maxsize=2, on_full="drop")

    async def streamer(_, ctx):
        for i in range(8):
            await ctx.emit("token", i=i)  # 边算边吐，像逐 token 输出那样
        return "流式输出结束"

    wf = Workflow(bus=bus)
    wf.add("stream", streamer, terminal=True)
    wf.entry("stream")

    result = await wf.run("开始")
    kept = []
    while not sub.queue.empty():
        kept.append((await sub.get())["i"])
    sub.close()

    print("结果：", result.output)
    print(f"队列保住的帧：{kept}｜主动丢弃的帧：{sub.dropped}")
    print("换成 on_full='block' 时，这里会等消费者取走才继续，也就是把压力传回上游。")


if __name__ == "__main__":
    asyncio.run(main())
