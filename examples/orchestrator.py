"""Service audit, orchestrator-worker — the fan-out width is data, not graph.

The graph has ONE reviewer node; how many times it runs is decided at runtime.
The planner agent calls the catalog tool, writes a numbered plan, and dispatch
turns each line into a Send — one copy of the reviewer template per service, all
concurrent in one wave. synth waits for every copy (the template-predecessor
rule, not join) and summarizes. Add a fifth service to the catalog tomorrow and
the same graph runs five reviewers without a line changed — with a real model;
the offline catalog/plan/findings fixtures do change together.

``build(lang)`` is the single assembly point (bilingual); ``main`` runs English.
Run: PYTHONPATH=. python3 examples/orchestrator.py
"""

import asyncio

from src import Agent, Workflow, send
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm


def build(lang: str = "en") -> Workflow:
    t = {
        "en": {
            "catalog": "deployed services: auth, payment, search, notification",
            "plan_text": "1. audit auth\n2. audit payment\n3. audit search\n4. audit notification",
            "plan_instruction": "You plan a service audit: call the catalog, then output one "
            "numbered line per service, 'N. audit <service>'.",
            "findings": {
                "auth": "p95 41ms, errors 0.0% — pass",
                "payment": "p95 188ms, errors 0.3% — pass, watch item",
                "search": "p95 320ms, errors 2.1% — FAIL: retry storm from a cold cache",
                "notification": "p95 65ms, errors 0.1% — pass",
            },
            "synth_line": "3 of 4 services pass; search fails on a retry storm — "
            "roll back the cache change before the release.",
            "synth_instruction": "You write the audit summary in two sentences.",
        },
        "zh": {
            "catalog": "已部署服务：auth、payment、search、notification",
            "plan_text": "1. 审计 auth\n2. 审计 payment\n3. 审计 search\n4. 审计 notification",
            "plan_instruction": "你规划一次服务巡检：先调目录工具，再按「N. 审计 <服务>」每服务一行编号输出。",
            "findings": {
                "auth": "p95 41ms，错误率 0.0% —— 通过",
                "payment": "p95 188ms，错误率 0.3% —— 通过，需关注",
                "search": "p95 320ms，错误率 2.1% —— 不通过：冷缓存引发重试风暴",
                "notification": "p95 65ms，错误率 0.1% —— 通过",
            },
            "synth_line": "4 个服务 3 个通过；search 因重试风暴不通过——发布前先回滚缓存变更。",
            "synth_instruction": "你用两句话写出巡检结论。",
        },
    }[lang]

    async def catalog(ctx=None):
        """List the services deployed in this environment."""
        return t["catalog"]

    wf = Workflow()
    planner = Agent(
        "planner",
        model=env_llm(ScriptedLlm([ToolCall("catalog", {}), t["plan_text"]])),
        instruction=t["plan_instruction"],
        tools=[catalog],
    )
    synth = Agent(
        "synth",
        model=env_llm(ScriptedLlm([t["synth_line"]])),
        instruction=t["synth_instruction"],
    )

    async def dispatch(plan_text, ctx):
        # Parse the planner's numbered text ('1. audit auth\n2. ...') into one
        # Send per line — the fan-out width is the model's output, not the graph.
        steps = []
        for line in plan_text.splitlines():
            head, _, rest = line.strip().partition(".")
            if head.strip().isdigit() and rest.strip():
                steps.append({"id": f"s{head.strip()}", "instruction": rest.strip()})
        return [send("reviewer", step, key=step["id"]) for step in steps]

    async def reviewer(step, ctx):
        # Offline fixture: `findings` covers exactly the catalog's four services
        # and the parser assumes 'audit <service>' — a real model that phrases a
        # line differently will miss the table (KeyError, by design: fail loud).
        service = step["instruction"].split(maxsplit=1)[1]
        return f"{service}: {t['findings'][service]}"

    wf.add_node("planner", planner)
    wf.add_node("dispatch", dispatch)
    wf.add_node("reviewer", reviewer, template=True)
    wf.add_node("synth", synth, terminal=True)  # template-predecessor rule (not join) waits for every copy
    wf.add_edge("planner", "dispatch")
    wf.add_edge("reviewer", "synth")
    wf.entry("planner")
    return wf


async def main():
    r = await build("en").run("Audit every deployed service before the release")
    print("Summary:", r.output)
    print(f"\nwaves: {r.metrics['waves']} (plan, dispatch, review, synthesize)")


if __name__ == "__main__":
    asyncio.run(main())
