"""After-sales refund — a supervisor whose "tools" are other agents.

What this file shows (two shapes of *call/delegation*):
- Supervisor (flat star): the supervisor never executes itself; each sub-agent
  is registered as a delegation tool — calling it runs that agent's own loop
  (its instruction, tools, context) and returns only the final answer (call
  semantics: dispatched out, result comes back).
- Hierarchical (tree): the risk specialist is itself a supervisor of a smaller
  expert, so one run grows a three-level delegation tree. Every level runs the
  same loop, there is no multi-agent engine, and each specialist sees only its
  own read-only tools — the tool set is the permission boundary between roles.

``build(lang)`` is the single assembly point (bilingual); ``main`` runs English.
Run: PYTHONPATH=. python3 examples/after_sales.py
"""

import asyncio

from src import Agent
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm


def build(lang: str = "en") -> Agent:
    t = {
        "en": {
            "invoice_fmt": "{order_id}: charged ¥399 on Sep 2, shipment never dispatched",
            "blacklist_fmt": "{order_id}: buyer clean, no blacklist hits",
            "related_fmt": "{order_id}: 1 related account, dormant, no fraud record",
            "related": {
                "script": [
                    ToolCall("query_related", {"order_id": "O-1234"}),
                    "One related account, dormant for 2 years, no fraud record.",
                ],
                "instruction": "You analyze accounts related to a buyer; answer in one sentence.",
            },
            "risk": {
                "script": [
                    ToolCall("query_blacklist", {"order_id": "O-1234"}),
                    ToolCall("related", {"task": "check accounts related to order O-1234"}),
                    "Blacklist clean; the one related account is dormant — risk is low.",
                ],
                "instruction": "You judge fraud and credit risk; check the data before concluding.",
            },
            "billing": {
                "script": [
                    ToolCall("query_invoice", {"order_id": "O-1234"}),
                    "Charged ¥399 on Sep 2 and the shipment never went out; refund due in full.",
                ],
                "instruction": "You answer billing facts only: invoices, payments, refunds.",
            },
            "sup": {
                "script": [
                    ToolCall("billing", {"task": "gather the billing facts of order O-1234"}),
                    ToolCall("risk", {"task": "assess the fraud risk of order O-1234"}),
                    "Billing confirms charged-but-unshipped and risk is low: "
                    "approve a full ¥399 refund.",
                ],
                "instruction": "You are the after-sales supervisor. You never execute yourself: "
                "dispatch the right specialist, wait for the answer, then decide.",
            },
        },
        "zh": {
            "invoice_fmt": "{order_id}: 9 月 2 日已扣款 399 元，商品始终未发货",
            "blacklist_fmt": "{order_id}: 买家干净，无黑名单命中",
            "related_fmt": "{order_id}: 关联账号 1 个，长期沉寂，无欺诈记录",
            "related": {
                "script": [
                    ToolCall("query_related", {"order_id": "O-1234"}),
                    "关联账号仅 1 个，已沉寂 2 年，无欺诈记录。",
                ],
                "instruction": "你分析买家的关联账号，一句话给出结论。",
            },
            "risk": {
                "script": [
                    ToolCall("query_blacklist", {"order_id": "O-1234"}),
                    ToolCall("related", {"task": "核查订单 O-1234 买家的关联账号"}),
                    "黑名单干净，唯一关联账号已沉寂——风险低。",
                ],
                "instruction": "你判断欺诈与信用风险，先查数据再下结论。",
            },
            "billing": {
                "script": [
                    ToolCall("query_invoice", {"order_id": "O-1234"}),
                    "9 月 2 日扣款 399 元且始终未发货，应全额退款。",
                ],
                "instruction": "你只回答账单事实：发票、支付、退款。",
            },
            "sup": {
                "script": [
                    ToolCall("billing", {"task": "查订单 O-1234 的账单事实"}),
                    ToolCall("risk", {"task": "评估订单 O-1234 的欺诈风险"}),
                    "账单确认扣款未发货、风险低：批准全额退款 399 元。",
                ],
                "instruction": "你是售后主管，自己不执行：挑对专家、等结果、再做决定。",
            },
        },
    }[lang]

    async def query_invoice(order_id, ctx):
        """Read the invoice and payment status of an order."""
        return t["invoice_fmt"].format(order_id=order_id)

    async def query_blacklist(order_id, ctx):
        """Check whether an order touches any blacklisted account."""
        return t["blacklist_fmt"].format(order_id=order_id)

    async def query_related(order_id, ctx):
        """List accounts related to the buyer of an order."""
        return t["related_fmt"].format(order_id=order_id)

    def expert(name, spec, tools=None, sub_agents=None):
        return Agent(
            name,
            model=env_llm(ScriptedLlm(list(spec["script"]))),
            instruction=spec["instruction"],
            tools=tools or [],
            sub_agents=sub_agents,
        )

    related = expert("related", t["related"], tools=[query_related])
    risk = expert("risk", t["risk"], tools=[query_blacklist], sub_agents=[related])
    billing = expert("billing", t["billing"], tools=[query_invoice])
    return expert(
        "supervisor", t["sup"], sub_agents=[billing, risk]
    )


async def main():
    supervisor = build("en")
    result = await supervisor.run("Order O-1234: customer charged but nothing shipped. Refund?")
    print("Decision:", result.output)
    print("\nWhat each delegation brought back:")
    for m in result.messages:
        if m.get("role") == "tool":
            print(f"  {m['name']} -> {m['content']}")
    print(
        f"\nsupervisor: {result.metrics['llm_calls']} thinks, "
        f"{result.metrics['tool_calls']} delegations — each specialist ran its own loop underneath"
    )


if __name__ == "__main__":
    asyncio.run(main())
