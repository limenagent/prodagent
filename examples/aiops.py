"""Incident response — parallel delegation (call) then a handoff (transfer).

- ``diagnose`` calls two diagnosing sub-agents in parallel on the shared
  scheduler: each checks its own read-only observability data, both results come
  back and merge into the root cause;
- ``decide`` then hands control to the repair agent with ``go`` and no return
  edge — transfer, not call: control leaves and the repair finishes there.

``build(lang)`` is the single assembly point (bilingual); ``main`` runs English.
Run: PYTHONPATH=. python3 examples/aiops.py
"""

import asyncio

from src import Agent, Workflow, go
from src.kernel import ToolCall
from src.runtime.agent import spawn_agent
from src.runtime.llm import ScriptedLlm, env_llm


def build(lang: str = "en") -> Workflow:
    t = {
        "en": {
            "cpu": "12:00 35% → 12:10 92% → 12:20 93% → 12:30 91% (spiking every ten minutes)",
            "log": "ERROR pool exhausted: connection wait timed out (5000ms), "
            "37 times in the last hour",
            "instruction_fmt": "You are {name}: check the data with tools before "
            "concluding, in two sentences.",
            "diag_cpu": [
                ToolCall("cpu_metrics", {}),
                "CPU saturates periodically every ten minutes; suspect queuing downstream.",
            ],
            "diag_log": [
                ToolCall("error_log", {}),
                "Error log shows connection-wait timeouts; the pool is exhausted.",
            ],
            "repairer": "Connection pool enlarged and upstream throttled; service recovered.",
            "ask_cpu": "check the CPU curve",
            "ask_log": "check the error log",
            "root_fmt": "root cause = pool exhaustion ({cpu}; {log})",
        },
        "zh": {
            "cpu": "12:00 35% → 12:10 92% → 12:20 93% → 12:30 91%（每十分钟打满一次）",
            "log": "ERROR pool exhausted: 获取连接超时（等待 5000ms），近 1 小时共 37 次",
            "instruction_fmt": "你是{name}，先用工具查数据再下结论，两句话内给出结论。",
            "diag_cpu": [
                ToolCall("cpu_metrics", {}),
                "CPU 每十分钟周期性打满，疑似下游排队。",
            ],
            "diag_log": [
                ToolCall("error_log", {}),
                "错误日志显示获取连接超时，连接池已耗尽。",
            ],
            "repairer": "已扩容连接池并对上游限流，服务恢复。",
            "ask_cpu": "看 CPU 曲线",
            "ask_log": "看错误日志",
            "root_fmt": "根因=连接池耗尽（{cpu}；{log}）",
        },
    }[lang]
    wf = Workflow()

    async def cpu_metrics(ctx=None):
        """Read the last hour's CPU curve."""
        return t["cpu"]

    async def error_log(ctx=None):
        """Read the recent error log."""
        return t["log"]

    def engineer(name, *script, tools=None):
        return Agent(
            name,
            model=env_llm(ScriptedLlm(list(script))),
            instruction=t["instruction_fmt"].format(name=name),
            tools=tools or [],
        )

    diag_cpu = engineer("diag_cpu", *t["diag_cpu"], tools=[cpu_metrics])
    diag_log = engineer("diag_log", *t["diag_log"], tools=[error_log])
    repairer = engineer("repairer", t["repairer"])

    async def diagnose(x, ctx):
        cpu, log = await asyncio.gather(
            spawn_agent(ctx, diag_cpu, t["ask_cpu"]),
            spawn_agent(ctx, diag_log, t["ask_log"]),
        )
        return go("decide", t["root_fmt"].format(cpu=cpu.get("output"), log=log.get("output")))

    async def decide(root, ctx):
        return go("repairer", root)  # transfer: no edge back

    wf.add_node("diagnose", diagnose)
    wf.add_node("decide", decide)
    wf.add_node("repairer", repairer, terminal=True)
    wf.add_edge("diagnose", "decide")
    wf.entry("diagnose")
    return wf


async def main():
    r = await build("en").run("API paging started failing at noon")
    print("Resolution:", r.output)
    print(f"waves: {r.metrics['waves']}")


if __name__ == "__main__":
    asyncio.run(main())
