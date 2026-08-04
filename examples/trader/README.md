# 奶茶代购

> 示例 #2 —— 多轮协商 + memory 驱动 replan + HITL 下单审批。

一个奶茶代购 Agent，帮用户订购下午茶——演示对话式多轮协商（提案 →
反驳 → 调整 → 下单）、memory 预置 constraint 驱动 replan、以及 HIGH
副作用工具的人工审批否决。场景贴近生活：人人都点过奶茶，"单价、杯数、
糖度、冰度"是真实决策维度，下单前确认是真实动作。

## 本示例展示什么

- **REACTIVE 多轮协商** —— agent 提具体订单方案（饮品/单价/杯数/糖度/冰度）
  → 用户反驳"太贵/要半糖" → agent 调整参数重提 → 收敛后调 `place_order`
  下单。不是一次性 plan，是探索性对话。
- **memory 驱动 replan** —— 预置 constraint（预算上限 200、起送 5 杯）
  recall 注入，agent 提方案时自动遵守；用户偏好（糖度/冰度）由 classify
  写入 memory，后续 turn recall 出来，agent 不重复问。
- **HITL 下单审批** —— `place_order` 是 HIGH 副作用（真实扣款不可逆），
  `ApprovalHooks` 门禁把它路由到人工审批。被拒的下单返回 `blocked_by`，
  永远不扣款。在 playground 里弹 approve/reject 对话框，CLI 里走 stdin
  提示。
- **MemoryHooks** —— demo 预置 memory 通过 MemoryHooks 注入，框架默认
  bundle 的 MemoryHooks 拿空 memory，demo 需要预置数据。

## 关键代码点

### `trader/agent.py` —— MemoryHooks 注入预置 constraint

```python
from prodagent.hooks.bundles.memory import MemoryHooks
from trader.memory import build_memory

agent = (
    Agent("trader", context=_SYSTEM_PROMPT, tools=[...], llm=llm)
    .reactive()
    .budget(turns=20, cost_usd=0.50, seconds=300.0)
    .extend(MemoryHooks(_build_memory(fw)))  # 注入预置 constraint
)
```

`_build_memory` 建带两条 constraint 的 MemoryManager：

```python
_CONSTRAINTS = [
    "预算上限 200 元,任何订单总价不得超过 200 元。",
    "起送 5 杯,任何订单杯数不得少于 5 杯。",
]
MemoryManager(framework_config=fw, constraints=list(_CONSTRAINTS))
```

MemoryHooks 在每 turn 开始时 recall 这两条 constraint，注入到 LLM 的
context 里，agent 提方案时自动遵守。

### `trader/tools.py` —— place_order 触发 HITL

```python
@tool(meta=ToolMeta(
    name="place_order",
    side_effect_level=SideEffectLevel.HIGH,  # ← 触发 HITL
    reversibility=0.1,
    resource_id="orders",
    enforced_idempotent=True,
))
async def place_order(proposal_id: str, idempotency_key: str = "") -> dict:
    """下单付款 —— 真实扣款,不可逆。"""
    ...
```

`HIGH` 副作用 + `reversibility=0.1` 让框架默认 bundle 的 ApprovalHooks
把 `place_order` 路由到人工审批。agent 调用它后 run 进入 SUSPENDED，
等用户 approve/reject。

## 为什么 REACTIVE

代购协商是对话循环 —— 没有前置 DAG。每 turn 看用户反馈后重新提方案。
Workflow DAG 一次性跑完，不适合协商。
