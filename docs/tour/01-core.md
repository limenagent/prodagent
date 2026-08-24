# ① 词汇 kernel

先看一个真实类型，再谈包。一次工具调用穿过整个框架时，它的形状是：

```python
# src/prodagent/kernel/types.py:53
@dataclass(slots=True)
class ToolCall:
    call_id: str
    name: str
    params: dict[str, Any]
```

三个字段。从 LLM 适配器到你的函数，"模型想调一个工具"在框架里始终是
这一个形状。

它住在 `kernel/`——一个独立成篇的包，七个模块、约两千一百行，
不 import 任何能力包。这件事有测试钉死（`tests/core/test_kernel_purity.py`
逐个加载 kernel 模块、断言 import 链上不出现
tooling/cognition/hooks/plan/coordination 中的任何一个）：往后每一站
讲的能力，都挂在内核之外的插槽上；内核自己只被依赖。

## 七个模块，一条阅读线

| 顺序 | 模块 | 内容 | 在线上的位置 |
|---|---|---|---|
| 1 | `kernel/types.py`（441 行） | `ToolCall` / `LLMResponse` / `ToolResult` / `ToolMeta` / `StopReason` / `RunState` / `ExecutionMode` / `SideEffectLevel` / `ToolOutcome` | 名词 |
| 2 | `kernel/events.py` | `AgentEvent` 家族——`chat()` 流式吐给你的事件 | 对外的动词 |
| 3 | `kernel/state.py` | `AgentRun`——一次调用的全部运行时状态 | 状态机的实体 |
| 4 | `kernel/bus.py` | 三协议总线：fire / check / collect | 外界观察与干预的唯一通道 |
| 5 | `kernel/budget.py` | `HardBudget` 上限 + `BudgetLedger` 共享账本 + `check_budget` | "停"的机制 |
| 6 | `kernel/step.py` | 原子：一次模型调用 + 至多一轮工具执行 | 能动性的最小单元 |
| 7 | `kernel/loop.py` | REACTIVE 循环——迭代原子的策略 | 第五站的主体 |

名词、事件、状态、总线、预算、原子、策略——自底向上正好一章读完。
`core/` 仍在（错误分类、配置、会话簿记这些"无行为的热心机械"），但
词汇已经搬进内核；两层的分界线就一条：**kernel 里的东西被一切依赖，
且不依赖任何能力**。

## AgentRun：一次调用的状态机

```python
# src/prodagent/kernel/state.py:136（节选）
@dataclass
class AgentRun(Generic[_RunT]):
    run_id: str
    task: str
    parent_run_id: str | None = None
    state: RunState = RunState.RUNNING
    messages: MessageList = field(default_factory=list)
    tool_history: list[ToolCall] = field(default_factory=list)
    pending_tool_call: ToolCall | None = None
    pending_approval_id: str | None = None
    ...
```

三个字段值得注意：

- **`state: RunState`** 只有四个值——`RUNNING / SUSPENDED / COMPLETED / FAILED`。
  状态机越小学越稳；"挂起等人审"是一等状态而不是异常，这是
  [审批](../topics/approval.md)能做成断点续跑的前提。
- **`pending_tool_call`** 记住"正卡在哪个调用上等人"。崩溃恢复后，循环
  重放的是**这一个调用**，不是重新问模型（`kernel/loop.py` 的
  resume 分支）。
- **`parent_run_id`** 让子 Agent 的花费可以向上汇总（
  [预算](../topics/budget.md) 的链式账本）。

## 停的机制：上限、账本、安全网

```python
# src/prodagent/kernel/budget.py:18
@dataclass
class HardBudget:
    max_turns: int = 20
    max_seconds: float = 120.0
    max_tokens: int = 100_000
    max_cost_usd: float = 1.0

# src/prodagent/kernel/budget.py:27
SAFETY_NET_BUDGET = HardBudget()
```

四轴独立、任一触顶即抛 `BudgetExceeded`。`SAFETY_NET_BUDGET` 是裸核
不配预算时的防跑飞底线——它**被执行**（否则裸循环可以无限烧钱，那是
bug 不是特性），但它不挂到 `AgentConfig`、不出现在用户配置里：
防跑飞属于循环的正确性，不属于用户的预算。

同一个文件里还住着 `BudgetLedger`：并发花钱的各方（spawn 出的子
Agent、peer 接力、ensemble 成员）共享同一本账，先 `reserve` 后
`commit`，在途的预留也占额度。一条 run 链从起点到终点只有这一本
账——[预算专题](../topics/budget.md)展开它的三段式语义。

## 错的词汇：一个根，两种严重度

`core/exceptions.py` 只有一个根 `AgentError`，之下按"谁处理"分：
`BudgetExceeded`（循环停下并落账）、`VersionConflict`（乐观并发输家）、
`SecurityViolation` 家族（拼进 `SECURITY_VETO_EXCEPTIONS`，消息平面里
变成一次 strict 拒绝）。整个文件 116 行——重构时删掉了 11 个没有 catch 位的异常类：
没人接的异常是死词汇。

## 取舍

**不是 pydantic 模型？** 运行时状态（`AgentRun`）用 dataclass：
框架内部单源流动的可变实体，不需要校验和序列化开销；只有跨进
持久化的地方（checkpoint）自己定义 `to_dict/from_dict`。pydantic 留给
真正需要验证外部输入的边界——`llm/` 适配器解析 provider 响应时。

**为什么给词汇一个独立包？** 曾经类型散在 `core/` 里，"循环"住
`runtime/`、驱动住 `coordination/`——读循环之前先要穿过协作机械。
现在内核物理上连续：读者从 `types.py` 走到 `loop.py` 不会撞见任何
一个能力包的名字。纯度不是审美口号，是会红的测试。

**为什么配置不做成环境变量优先？** `FrameworkConfig.from_env()` 存在，
但显式构造 `AgentConfig(framework=...)` 永远优先于环境——库不该让
进程里的两个 Agent 因为共享环境变量而互相污染（playground 给每个示例
注入带独立 namespace 的 config，正是为了这个）。
