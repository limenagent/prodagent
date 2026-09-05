"""07 故障应急 —— 并行派诊断子 Agent 查清根因（call），再接力交给修复 Agent（transfer）。

两种多 Agent 协作语义在同一个 Workflow 里同时出现：
- call（委派要返回）：diagnose 节点并行 delegate 两个诊断 Agent，结果都要回来；
- transfer（接力不回头）：根因明确后，decide 节点 go 到修复 Agent 节点，
  图上不画回边，由修复 Agent 用自己的模型接手并直接收尾，不再返回诊断流程。

跑法：PYTHONPATH=. python3 examples/07_aiops.py
"""

import asyncio

from src import Agent, Workflow, go
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm


async def main():
    # 只读的观测工具：诊断 Agent 有数据可查，而不是凭一句“看 CPU 曲线”瞎猜。
    async def cpu_metrics(ctx=None):
        """读取最近一小时的 CPU 曲线。"""
        return "12:00 35% → 12:10 92% → 12:20 93% → 12:30 91%（每十分钟打满一次）"

    async def error_log(ctx=None):
        """读取最近的错误日志。"""
        return "ERROR pool exhausted: 获取连接超时（等待 5000ms），近 1 小时共 37 次"

    # 配了 OPENAI_API_KEY 用真实模型，没配按离线脚本跑（与 playground 同一开关）。
    def engineer(name, *script, tools=None):
        return Agent(
            name,
            model=env_llm(ScriptedLlm(list(script))),
            instruction=f"你是{name}，先用工具查数据再下结论，两句话内给出结论。",
            tools=tools or [],
        )

    diag_cpu = engineer(
        "diag_cpu",
        ToolCall("cpu_metrics", {}),
        "CPU 每十分钟周期性打满，疑似下游排队。",
        tools=[cpu_metrics],
    )
    diag_log = engineer(
        "diag_log",
        ToolCall("error_log", {}),
        "错误日志显示获取连接超时，连接池已耗尽。",
        tools=[error_log],
    )
    repairer = engineer("repairer", "已扩容连接池并对上游限流，服务恢复。")

    wf = Workflow()

    async def diagnose(x, ctx):
        # call：两个诊断子 Agent 并行，结果都要回来汇总。
        cpu, log = await asyncio.gather(
            diag_cpu.delegate("看 CPU 曲线"), diag_log.delegate("看错误日志")
        )
        root = f"根因=连接池耗尽（{cpu}；{log}）"
        return go("decide", root)

    async def decide(root, ctx):
        # transfer：go 到修复 Agent 节点；图上不画回边，交出去就由它收尾、不再回来。
        return go("repairer", root)

    wf.add("diagnose", diagnose)
    wf.add("decide", decide)
    wf.add("repairer", repairer, terminal=True)   # Agent 可以直接当图上的节点
    wf.edge("diagnose", "decide")
    wf.entry("diagnose")
    # 能交给谁，就是图上 add 了哪些 Agent 节点；repairer 没有出边，接力到此为止。

    result = await wf.run("订单服务延迟飙升")
    print("诊断与处置结果：", result.output)


if __name__ == "__main__":
    asyncio.run(main())
