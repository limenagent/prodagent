"""06 行程规划 —— 主流程一次性并行派三个专业子 Agent，再汇合合成行程书。

三个子 Agent（排行程、订餐厅、查交通）作为 Workflow 的并行节点，在同一波并发
执行、各用各的模型；合成节点等三个都完成（join="all"）后汇总。这就是 call 语义：
派出去、结果都要交回来。

跑法：PYTHONPATH=. python3 examples/06_trip_planner.py
"""

import asyncio

from src import Agent, Workflow
from src.runtime.llm import ScriptedLlm, env_llm


def specialist(name, line):
    # 每个专业 Agent 用固定脚本扮演，离线可跑；换成真实模型即可。
    return Agent(name, model=env_llm(ScriptedLlm([line])), instruction=f"你负责{name}")


async def main():
    itinerary = specialist("itinerary", "第一天外滩、第二天迪士尼")
    dining = specialist("dining", "本帮菜晚餐已预留")
    traffic = specialist("traffic", "地铁 2 号线接驳，备打车方案")

    wf = Workflow()
    wf.add("itinerary", itinerary)
    wf.add("dining", dining)
    wf.add("traffic", traffic)

    async def synth(parts, ctx):
        return "行程书已生成：\n- " + "\n- ".join(parts.values())
    wf.add("synth", synth, join="all", terminal=True)

    wf.entry("itinerary", "dining", "traffic")     # 三个子 Agent 同一波并行
    wf.edge("itinerary", "synth")
    wf.edge("dining", "synth")
    wf.edge("traffic", "synth")

    result = await wf.run("上海两日游")
    print(result.output)
    print(f"总波次 {result.metrics['waves']}（三个子 Agent 在同一波并行）")


if __name__ == "__main__":
    asyncio.run(main())
