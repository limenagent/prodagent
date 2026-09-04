"""playground 内置演示：客服退款。

一条很能体现框架价值的链路：客服 Agent 先调只读工具查订单、给出退款建议；
真要动钱时，流程在 approve 节点 wait_human 挂起，网页上点“批准/拒绝”后继续。

- 配了 OPENAI_API_KEY：用真实的 OpenAI 兼容模型；
- 没配：用内置脚本模型离线演示，零配置也能完整点一遍“对话→审批→继续”。
"""

from __future__ import annotations

from src import Agent, Workflow, go, wait_human
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm

# 假装的订单库，真实项目里这里会去查数据库/下游服务。
_ORDERS = {
    "O-1234": {"status": "已超时3天未发货", "amount": 88},
    "O-5678": {"status": "已签收", "amount": 120},
}


async def query_order(order_id, ctx):
    """按订单号查询订单状态与金额。"""
    order = _ORDERS.get(order_id, {"status": "查无此单", "amount": 0})
    return f"订单 {order_id}：{order['status']}，金额 {order['amount']} 元"


async def _refund(suggestion, ctx):
    return f"已按审批结果执行退款。{suggestion}"


async def _deny(suggestion, ctx):
    return f"审批未通过，已关闭退款单并告知用户。{suggestion}"


def _default_model():
    # 离线脚本：先查单，再给建议——和真实模型的两轮行为一致。
    return ScriptedLlm(
        [
            ToolCall("query_order", {"order_id": "O-1234"}),
            "订单 O-1234 已超时 3 天未发货，按政策建议退款 88 元。",
        ]
    )


def build_demo(model=None) -> Workflow:
    model = model or env_llm(_default_model())
    wf = Workflow(model=model)
    support = Agent(
        name="support",
        model=model,
        instruction="你是售后客服。先用 query_order 查订单，再判断是否应退款，"
        "用一句话给出建议和金额，不要自行决定退款。",
        tools=[query_order],
        bus=wf.bus,
    )  # 子 Agent 事件汇入同一总线

    async def approve(suggestion, ctx):
        if ctx.resume_value is None:
            # 第一次到这里：还没问过人，先真正停下来，把建议交出去等审批。
            return wait_human("该订单建议退款，是否批准？", {"suggestion": suggestion})
        if ctx.resume_value.get("approved"):
            return go("refund", suggestion, decision="批准")
        return go("deny", suggestion, decision="拒绝")

    wf.add("support", support)
    wf.add("approve", approve)
    wf.add("refund", _refund, terminal=True)
    wf.add("deny", _deny, terminal=True)
    wf.edge("support", "approve")
    wf.edge("approve", "refund")
    wf.edge("approve", "deny")
    wf.entry("support")
    return wf
