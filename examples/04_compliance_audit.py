"""04 合规审计 —— 并行核查 + 写操作人工审批，被拒后只改动作、不推倒重来。

两条核查分支并行跑，汇合后出结论；“冻结账户”要真正停下来等人批准（wait_human）。
若没批，流程不重跑前面的核查，只是沿另一条边走到报告，标注“未获批准”。

跑法：PYTHONPATH=. python3 examples/04_compliance_audit.py
"""

import asyncio

from src import Workflow, go, wait_human


def build_audit_workflow():
    wf = Workflow()

    async def screen_suspicious(x, ctx):
        return {"flags": "发现快进快出交易"}

    async def screen_accounts(x, ctx):
        return {"links": "关联到 3 个同源账户"}

    async def synthesize(x, ctx):
        s = ctx.shared
        return f"综合判断：{s['flags']}；{s['links']}，建议冻结。"

    async def freeze(summary, ctx):
        if ctx.resume_value is None:
            # 第一次到这里：还没问过人，先挂起，把待确认信息一并交出去。
            return wait_human("批准冻结这些账户吗？", {"accounts": ["A1", "A2"]})
        if not ctx.resume_value.get("approved"):
            return go("report", summary, decision="建议冻结，但本次未获批准")
        return go("report", summary, decision="已冻结 A1、A2")

    async def report(summary, ctx):
        return f"{summary}｜处置：{ctx.shared['decision']}"

    wf.add("screen_suspicious", screen_suspicious)
    wf.add("screen_accounts", screen_accounts)
    wf.add("synthesize", synthesize, join="all")
    wf.add("freeze", freeze)
    wf.add("report", report, terminal=True)
    wf.entry("screen_suspicious", "screen_accounts")  # 两个入口同波并行
    wf.edge("screen_suspicious", "synthesize")
    wf.edge("screen_accounts", "synthesize")  # join=all：两条都到才汇合
    wf.edge("synthesize", "freeze")
    wf.edge("freeze", "report")
    return wf


async def main():
    wf = build_audit_workflow()

    first = await wf.run("审计账户 A1")
    print("第一次运行状态：", first.status, "—— 已挂起等待审批")

    # 人看了材料后选择“不批准冻结”，从挂起点恢复，前面的核查结果都还在。
    second = await wf.resume(first.run_id, {"approved": False})
    print("恢复后最终报告：", second.output)


if __name__ == "__main__":
    asyncio.run(main())
