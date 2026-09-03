"""奶茶代购 Agent —— 多轮协商 + memory 驱动 replan + HITL 下单审批。

本示例展示:
  - **REACTIVE 多轮协商** —— agent 提具体订单方案(饮品/单价/杯数/糖度/冰度)
    → 用户反驳"太贵/要半糖" → agent 调整参数重提 → 收敛后调 ``place_order``
    下单。不是一次性 plan,是探索性对话。
  - **chat 多轮累积** —— ``agent.chat(message, run_id=...)`` 同一 run_id
    多轮,LLM 看到完整对话历史 + memory recall 注入。
  - **memory 驱动 replan** —— 预置 constraint(预算上限、起送杯数)recall
    注入,agent 提方案时自动遵守;用户偏好(糖度/冰度)由 classify 写入
    memory,后续 turn recall 出来,agent 不重复问。
  - **HITL 下单审批** —— ``place_order`` 是 HIGH 副作用(真实扣款不可逆),
    框架默认 bundle 的 ApprovalHooks 把它路由到人工审批 —— playground 里
    弹 approve/reject 对话框,CLI 里走 stdin 提示。被拒的下单返回
    ``blocked_by``,永远不扣款。

为什么 REACTIVE: 代购协商是对话循环 —— 没有前置 DAG。每 turn 看用户反馈
后重新提方案。Workflow DAG 一次性跑完,不适合协商。
"""

from __future__ import annotations

from prodagent import (
    Agent,
    AgentConfig,
    FrameworkConfig,
    HardBudget,
    LLMClient,
    MemoryManager,
    script,
    use_fake_llm,
)
from prodagent.hooks.bundles.memory import MemoryHooks

from trader.tools import place_order, propose_order

_SYSTEM_PROMPT = """\
你是奶茶代购 Agent,帮用户订购奶茶(公司下午茶、朋友聚会等场景)。

## 工作流
1. 调 ``propose_order(drink, unit_price, quantity, sugar, ice)`` 提一个\
具体方案 —— 要给具体饮品名、单价、杯数、糖度、冰度,不要空泛说"我便宜点"。
2. 等用户回应。如果用户反驳("太贵"/"要半糖"/"换饮品"),调整参数重提 \
``propose_order`` —— 不要重复之前被否的方案,记住用户说的偏好(糖度/冰度)。
3. 用户同意后调 ``place_order`` 下单 —— 把上一轮 ``propose_order`` 返回的\
``proposal_id`` 和方案参数(drink/quantity/price/sugar/ice)一起传进去,\
不要只传 proposal_id。审批对话框会显示这些参数给人工审。

## 规则
- 预算上限 200 元,起送 5 杯 —— memory 里有这两条 constraint,提方案时\
不要违反。
- 每次提方案都要调 ``propose_order``,不要只在文本里说"我降到 X 元"。
- 用户说偏好(如"半糖少冰")后,后续方案要带上,不要反复问。
- 下单前确认用户明确同意("可以"/"下单"/"确认"),不要自作主张下单。
- **下单时直接调 ``place_order``,不要在文本里请求用户确认**。HIGH \
副作用工具会被框架自动 suspend 等人工审批,你不需要等待或询问。
"""


def _script_negotiation_llm() -> LLMClient:
    """三轮奶茶协商 FakeLLM 脚本 —— 提案(超预算) → 调整(偏好) → 下单(HITL)。

    Turn 1: agent 提案 珍珠奶茶 22元×10杯=220(超预算 200)
    Turn 2: 用户"超了,便宜点,要半糖少冰" → agent 调整 杨枝甘露 18元×10杯=180,半糖少冰
    Turn 3: 用户"可以,下单" → agent 调 place_order(触发 HITL 审批)

    三轮串成一个 run 的多次 LLM 调用,每次 chat() 追加 user message 后 run。
    """
    return script(
        # Turn 1: 提方案(超预算)
        {"tool": "propose_order", "params": {
            "drink": "珍珠奶茶",
            "unit_price": 22.0,
            "quantity": 10,
            "sugar": "全糖",
            "ice": "正常冰",
        }},
        {"content": (
            "我提个方案:珍珠奶茶,单杯 22 元 × 10 杯,全糖正常冰,"
            "总价 220 元。您看怎么样?"
        )},
        # Turn 2: 用户"超预算,要半糖少冰" → 调整
        {"tool": "propose_order", "params": {
            "drink": "杨枝甘露",
            "unit_price": 18.0,
            "quantity": 10,
            "sugar": "半糖",
            "ice": "少冰",
        }},
        {"content": (
            "好的,我调整一下:换成杨枝甘露,单杯降到 18 元 × 10 杯,"
            "半糖少冰,总价 180 元,在预算内。这个可以吗?"
        )},
        # Turn 3: 用户"下单" → place_order(触发 HITL)
        # place_order 接全参数:proposal_id 作审计锚点,drink/quantity/price 是
        # 真实业务数据 —— LLM 从 Turn 2 的 tool_result 读出来显式传进来。
        {"tool": "place_order", "params": {
            "proposal_id": "PROP-0002",
            "drink": "杨枝甘露",
            "unit_price": 18.0,
            "quantity": 10,
            "sugar": "半糖",
            "ice": "少冰",
        }},
        {"content": "已下单!订单 ORD-0001(杨枝甘露 ×10,半糖少冰,总价 180 元,经人工审批批准)。预计 30 分钟送达。"},
    )


def _build_memory(framework_config: FrameworkConfig) -> MemoryManager:
    """建带预置 constraint 的 memory(预算上限 200、起送 5 杯)。

    constraint 通过 ``MemoryManager(constraints=[...])`` 预置,不需要
    async seed —— 见 ``trader/memory.py``。
    """
    from trader.memory import build_memory

    return build_memory(framework_config=framework_config)


DEFAULT_TASK = "订 10 杯奶茶,公司下午茶,预算 200 以内。提个方案吧。"


def build_trader_agent(
    *,
    memory: MemoryManager | None = None,
    framework_config: FrameworkConfig | None = None,
    run_id: str | None = None,
) -> Agent:
    """组装奶茶代购 Agent。

    Args:
        memory: 预 seeded 的 MemoryManager(demo 用)。不传时建带预置
            constraint 的 memory(预算上限 200、起送 5 杯)。
        framework_config: 父 fw;不传时用默认。
        run_id: playground HITL 工厂注入;trader 走 chat 路径自己管
            session_id,这里收下但不使用。
    """
    from prodagent.base.config import production

    fw = framework_config or production()
    resolved_memory = memory or _build_memory(fw)
    use_fake = use_fake_llm()
    llm = _script_negotiation_llm() if use_fake else None

    return Agent(
        "trader",
        system_prompt=_SYSTEM_PROMPT,
        tools=[propose_order, place_order],
        budget=HardBudget(max_turns=20, max_cost_usd=0.50, max_seconds=300.0),
        config=AgentConfig(
            name="trader",
            llm=llm,
            framework=fw,
            extensions=[MemoryHooks(resolved_memory)],
        ),
    )
