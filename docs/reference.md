# API 参考

> 自动生成的 API 文档。本页是入口，详细接口由 mkdocstrings 从源码 docstring 自动生成。

---

## 核心入口

### Agent

Agent 是框架的主要入口。一个 Agent 有身份、系统提示、工具集和执行模式。

**导入**：
```python
from prodagent import Agent, AgentConfig, ExecutionMode
```

**关键参数**：
- `name: str` — Agent 身份标识
- `system_prompt: str` — 系统提示
- `tools: list[Tool]` — 可用工具列表
- `mode: ExecutionMode` — REACTIVE / PLAN_FIRST / WORKFLOW
- `config: AgentConfig` — 完整配置（含框架配置、LLM、预算等）

**主要方法**：
- `async chat(task: str, *, run_id: str | None = None) -> RunResult` — 执行一次任务
- `async stream(task: str, *, run_id: str | None = None) -> AsyncGenerator[AgentEvent]` — 流式执行

---

## 工具定义

### @tool 装饰器

```python
from prodagent import tool, SideEffectLevel

@tool(name="...", readonly=True, side_effect=SideEffectLevel.HIGH)
async def my_tool(param1: str, param2: int = 5) -> str:
    """工具描述，会被模型看到。

    Args:
        param1: 参数说明
        param2: 参数说明
    """
    ...
```

**参数**：
- `name: str` — 工具名（默认函数名）
- `description: str` — 工具描述（默认 docstring）
- `readonly: bool` — 是否只读（只读工具可并行）
- `side_effect: SideEffectLevel` — 副作用等级
- `timeout: float | None` — 单工具超时
- `tags: list[str]` — 标签（用于检索和权限分组）

---

## 执行模式

```python
from prodagent import ExecutionMode

ExecutionMode.REACTIVE     # 边走边想，默认
ExecutionMode.PLAN_FIRST   # 先规划 DAG 后执行
ExecutionMode.WORKFLOW     # 静态预定义 DAG
```

---

## 预算配置

```python
from prodagent import HardBudget

budget = HardBudget(
    max_turns=20,        # 最多轮数
    max_seconds=120.0,   # 最多秒数
    max_tokens=100_000,  # 最多 billable token
    max_cost_usd=1.0,    # 最多美元
)
```

多 Agent 共享预算：
```python
from prodagent import BudgetLedger

ledger = BudgetLedger(max=budget)
await ledger.reserve(member="agent-a", cost_usd=0.3)
await ledger.commit(member="agent-a", cost_usd=0.25, reserved_cost_usd=0.3)
```

---

## 配置

### production() 一键全套

```python
from prodagent.core.config import production

config = AgentConfig(
    name="demo",
    framework=production(),  # 落盘恢复 + span追踪 + 审批门 + 权限 + 缓存 + 压缩
)
```

### 自定义配置

```python
from prodagent import FrameworkConfig
from prodagent.backends.postgres import PostgresCheckpointConfig

config = FrameworkConfig(
    checkpoint=PostgresCheckpointConfig(dsn="..."),
    budget=HardBudget(max_turns=50),
    # 未配置的用默认值（file/memory）
)
```

---

## 模型配置

```python
from prodagent import LLMConfig, FakeLLMAdapter

# OpenAI 兼容端点
config = LLMConfig(model="deepseek-chat", temperature=0.0)

# FakeLLM（离线测试）
fake = FakeLLMAdapter(responses=[
    LLMResponse(content="你好", stop_reason="end_turn"),
])
```

环境变量配置：
- `USE_FAKE_LLM=1` — 使用 FakeLLM
- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` — OpenAI 兼容端点
- `ANTHROPIC_API_KEY` — Anthropic 原生

---

## 多 Agent 协作

```python
from prodagent import Ensemble, WorkQueue, Board, TerminationPolicy, MaxRounds

# 委派
from prodagent.coordination.spawn import Spawn
spawn = Spawn(agents=[worker1, worker2], llm=planner, ctx=parent_runtime)

# 接力
from prodagent.coordination.peer import PeerChain
chain = PeerChain([researcher, writer, reviewer])

# 投票
ensemble = Ensemble(agents=[a, b, c], strategy=Moderated(judge=judge))

# 黑板
board = Board()
board.add_agent(agent, triggers=[Trigger.on_topic("data_ready")])

# 工作队列
queue = WorkQueue(workers=[w1, w2, w3])
```

---

## 事件类型

```python
from prodagent.kernel.events import (
    RunCompletedEvent,
    RunFailedEvent,
    RunSuspendedEvent,
    ThinkTokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
```

流式执行时，`agent.stream()` 返回这些事件的异步生成器。

---

## 异常体系

```python
from prodagent import (
    BudgetExceeded,          # 预算耗尽
    InfiniteLoopDetected,    # 死循环检测
    VersionConflict,         # checkpoint 乐观并发冲突
    SecurityViolation,       # 越权操作
    PromptInjectionDetected, # 提示注入检测
    SensitiveContentDetected,# 敏感内容检测
    CorruptedCheckpointError,# checkpoint 损坏
)
```

---

## 完整 API 文档

以下模块的详细接口由 mkdocstrings 自动生成，包含所有 public class、method、function 的签名、参数、返回值和 docstring：

- `prodagent.runtime.agent` — Agent 类
- `prodagent.kernel.loop` — ReactiveLoop
- `prodagent.kernel.step` — Step
- `prodagent.kernel.budget` — HardBudget / BudgetLedger
- `prodagent.kernel.types` — 类型定义
- `prodagent.tooling.decorator` — @tool 装饰器
- `prodagent.tooling.base` — FunctionTool
- `prodagent.tooling.dispatcher` — ToolDispatcher
- `prodagent.ports.*` — 所有端口定义
- `prodagent.coordination.*` — 多 Agent 协作
- `prodagent.cognition.*` — 上下文压缩与记忆
- `prodagent.hooks.*` — 审批、权限、可观测

> 运行 `mkdocs serve` 后，本页下方会自动渲染上述模块的完整 API 文档。

---

## 回到

- [学习路线首页 →](tour/index.md)
- [设计取舍 →](decisions.md)
- [术语表 →](glossary.md)
