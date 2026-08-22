# ① 词汇 core

先看一个真实类型，再谈包。一次工具调用穿过整个框架时，它的形状是：

```python
# src/prodagent/core/types.py:53
@dataclass(slots=True)
class ToolCall:
    call_id: str
    name: str
    params: dict[str, Any]
```

三个字段。从 LLM 适配器到你的函数，“模型想调一个工具”在框架里始终是
这一个形状。`core/` 不执行任何东西，它定义所有层共说的语言——后面
每一站都在 import 它。

## 这个包里有什么

| 文件 | 内容 | 为什么是“词汇” |
|---|---|---|
| `core/types.py`（440 行） | `ToolCall` / `LLMResponse` / `ToolResult` / `ToolMeta` / `StopReason` / `RunState` / `ExecutionMode` / `SideEffectLevel` / `ToolOutcome` | 数据形状 + 状态词汇 |
| `core/state/run.py` | `AgentRun`——一次调用的全部运行时状态 | 状态机的实体 |
| `core/state/session.py` | `ConversationSession`——多轮对话的根 | 轮次簿记 |
| `core/budget.py` | `HardBudget` + `check_budget` + `SAFETY_NET_BUDGET` | “停”的词汇 |
| `core/exceptions.py` | 18 个异常类 + `SECURITY_VETO_EXCEPTIONS` | “错”的词汇 |
| `core/config.py` | `FrameworkConfig` 及四个子配置 + `production()` | 配置词汇（也是裸核/生产的开关所在） |
| `core/events.py` | `AgentEvent` 家族——`chat()` 流式吐给你的事件 | 对外词汇 |

## AgentRun：一次调用的状态机

```python
# src/prodagent/core/state/run.py:136（节选）
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
  状态机越小学越稳；“挂起等人审”是一等状态而不是异常，这是
  [审批](../topics/approval.md)能做成断点续跑的前提。
- **`pending_tool_call`** 记住“正卡在哪个调用上等人”。崩溃恢复后，循环
  重放的是**这一个调用**，不是重新问模型（`runtime/reactive.py` 的
  resume 分支）。
- **`parent_run_id`** 让子 Agent 的花费可以向上汇总（
  [预算](../topics/budget.md) 的树形账本）。

## 停的词汇：HardBudget 与安全网

```python
# src/prodagent/core/budget.py:18
@dataclass
class HardBudget:
    max_turns: int = 20
    max_seconds: float = 120.0
    max_tokens: int = 100_000
    max_cost_usd: float = 1.0

# src/prodagent/core/budget.py:27
SAFETY_NET_BUDGET = HardBudget()
```

四轴独立、任一触顶即抛 `BudgetExceeded`。`SAFETY_NET_BUDGET` 是裸核
不配预算时的防跑飞底线——它**被执行**（否则裸循环可以无限烧钱，那是
bug 不是特性），但它不挂到 `AgentConfig`、不出现在用户配置里：
防跑飞是循环的正确性，不是你的预算。

## 错的词汇：一个根，两种严重度

`core/exceptions.py` 只有一个根 `AgentError`，之下按“谁处理”分：
`BudgetExceeded`（循环停下并落账）、`VersionConflict`（乐观并发输家）、
`SecurityViolation` 家族（拼进 `SECURITY_VETO_EXCEPTIONS`，消息平面里
变成一次 strict 拒绝）。整个文件 116 行——重构时删掉了 11 个没有 catch 位的异常类：
没人接的异常是死词汇。

## 取舍

**不是 pydantic 模型？** 运行时状态（`AgentRun`）用 dataclass：
它是框架内部单源流动的可变实体，不需要校验和序列化开销；只有跨进
持久化的地方（checkpoint）自己定义 `to_dict/from_dict`。pydantic 留给
真正需要验证外部输入的边界——`llm/` 适配器解析 provider 响应时。

**为什么配置不做成环境变量优先？** `FrameworkConfig.from_env()` 存在，
但显式构造 `AgentConfig(framework=...)` 永远优先于环境——库不该让
进程里的两个 Agent 因为共享环境变量而互相污染（playground 给每个示例
注入带独立 namespace 的 config，正是为了这个）。

