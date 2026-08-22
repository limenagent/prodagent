# 预算

模型在两个工具之间反复横跳，token 一分钟烧一百块。怎么在 turns /
seconds / tokens / cost 四个维度同时设硬上限，让循环在任何方向失控
都撞墙？这是 `HardBudget` 的全部工作。

## 四个轴，任一触顶即停

```python
# src/prodagent/core/budget.py:18
@dataclass
class HardBudget:
    max_turns: int = 20
    max_seconds: float = 120.0
    max_tokens: int = 100_000
    max_cost_usd: float = 1.0
```

`check_budget`（`core/budget.py:30`）在循环的每个关键点被调：think 前、
decide 后、execute 后。触顶抛 `BudgetExceeded`，循环以 FAILED 落账——
带着已发生的全部记账，不是静默消失。

四个轴为什么缺一不可：turns 防循环、seconds 防挂死、tokens/cost 防
**单轮巨贵**（一次 100 万 token 的上下文，20 轮限制救不了你）。cost 轴
开箱即准——`llm/pricing.py` 的目录在 `LLMConfig` 初始化时自动填价
（查不到的模型计零价，宁可免费也不谎报）。

## 安全网：防跑飞 ≠ 你的预算

```python
# src/prodagent/core/budget.py:27
SAFETY_NET_BUDGET = HardBudget()
```

裸核不配 `budget=` 时跑在这份默认上。注意它是什么、不是什么：
**是**循环的防跑飞底线（没有它，一个坏掉的 FakeLLM 死循环可以烧穿
任何真实账号）、四轴全执行、也是 LLM 调用超时的推导基准；**不是**
用户配置——不挂 `AgentConfig`、不出现在你的配置里。给了 `budget=`
就全轴覆盖。一个能无限烧钱的裸循环是 bug，不是“给用户自由”。

## 树形账本：子 Agent 的花费实时汇总

spawn 出去的子 Agent 花的钱算谁的？算树的。两条机制：

- **`SpawnAccumulator`**（`coordination/accounting.py:67`）挂在父的
  执行器上，子 run 结束时把 turns/tokens/cost 折回父账
  （`fold_spawn_accounting`，`accounting.py:46`）。
- **`BudgetLedger`**（`coordination/budget_ledger.py:61`）管 peer 链与
  舞台原语：接力前 commit 当前成员的花销，交接前 check 下家的余量——
  链上任何一环超限，链条在那里停下。

于是预算天然是**层级**的：父的 `max_cost_usd` 是整棵树的封顶，子可以
有自己的更小预算，但不能突破父的。多 Agent 系统的成本失控多数不是
单个 Agent 贵，是**没人看着总量**——树形账本就是那个看总量的人。

## 时间轴怎么执行

`max_seconds` 不是事后统计：`ReactiveLoop._call_llm` 把“剩余时间”
直接设为该次 LLM 调用的 `asyncio.wait_for` 超时。时间预算触顶的形态
是那一次调用被掐断，然后统一落成 `BudgetExceeded(axis="seconds")`。
工具超时则是 `ToolMeta.timeout_seconds` 的事——粒度不同，各管各的。

## 取舍

**为什么不用“预算用尽先警告再停”？** 因为警告是给人的，Agent 没有
看警告的循环。预算的全部价值在于**不可协商**；一旦存在“再跑一轮”
的余地，失控路径就重新打开。需要软限制的话，监听 `TOKEN_UPDATE`
事件（hook 总线每轮都发）自己做——框架提供硬闸，软策略是你的。

