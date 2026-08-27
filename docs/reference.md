# API 参考

> 核心 API 速查。详细接口签名和 docstring 请参考源码或运行 `mkdocs serve` 查看自动生成文档。

---

## 核心入口

### Agent

```python
from prodagent import Agent, AgentConfig, ExecutionMode

agent = Agent(
    "demo",                                    # name（位置参数，必填）
    system_prompt="你是一个 helpful assistant",  # 系统提示
    tools=[search, fetch],                     # 可用工具
    mode=ExecutionMode.REACTIVE,               # REACTIVE / PLAN_FIRST
    budget=HardBudget(max_turns=20),           # 四轴预算
    workflow=wf,                               # 可选：手写 Workflow（自动设为 PLAN_FIRST）
    allow_replan=True,                         # PLAN_FIRST 模式下是否允许增量重规划
    config=AgentConfig(name="demo"),           # 完整配置（LLM、后端、hooks 等）
)
```

**主要方法**：

| 方法 | 返回 | 说明 |
|------|------|------|
| `await chat(task, *, run_id=None, session_id=None, **kwargs)` | `RunResult` | 执行一次任务，返回最终结果 |
| `stream(task, *, run_id=None, session_id=None, **kwargs)` | `AsyncGenerator[AgentEvent]` | 流式执行，逐事件产出 |

> **注意**：Agent 构造函数没有 `llm` 参数。LLM 客户端通过 `AgentConfig(llm=...)` 传入。

### AgentConfig

```python
@dataclass
class AgentConfig:
    name: str
    system_prompt: str = ""
    tools: list[Tool] = field(default_factory=list)
    mode: ExecutionMode = ExecutionMode.REACTIVE
    budget: HardBudget = field(default_factory=HardBudget)
    llm: LLMClient | None = None               # LLM 客户端或 LLMConfig
    framework: FrameworkConfig | None = None   # 框架配置（后端、压缩等）
    hooks: HookRegistry | None = None          # 事件总线
    agents: list[Agent] = field(default_factory=list)   # spawn 子 Agent
    peers: list[Agent] = field(default_factory=list)    # peer 接力
    memory: MemoryProvider | None = None       # 记忆系统
    skills: SkillRegistry | None = None        # 技能注册表
    initial_plan: Plan | None = None           # 预编译计划
    # ... 更多字段见 runtime/config.py
```

---

## 工具定义

### @tool 装饰器

```python
from prodagent import tool
from prodagent.kernel.types import SideEffectLevel, ToolMeta

# 只读工具
@tool(name="search", readonly=True)
async def search(query: str, max_results: int = 5) -> str:
    """搜索网络信息。"""
    ...

# 高副作用工具（通过 meta 设置）
@tool(
    name="send_email",
    meta=ToolMeta(
        name="send_email",
        side_effect_level=SideEffectLevel.HIGH,
        timeout_seconds=30.0,
        domain="communication",
        enforced_idempotent=True,
    ),
)
async def send_email(to: str, body: str) -> str:
    ...
```

**@tool 参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `name` | `str \| None` | 工具名（默认函数名） |
| `description` | `str \| None` | 工具描述（默认 docstring） |
| `readonly` | `bool` | 是否只读（可并行，隐含 LOW 副作用） |
| `meta` | `ToolMeta \| None` | 完整元数据 |

> **注意**：`@tool` 没有 `side_effect`、`timeout`、`tags` 参数。副作用等级和超时通过 `meta=ToolMeta(...)` 设置。

### ToolMeta

```python
@dataclass
class ToolMeta:
    name: str
    is_readonly: bool = False
    side_effect_level: SideEffectLevel = SideEffectLevel.LOW
    enforced_idempotent: bool = False
    timeout_seconds: float = 10.0
    domain: str = "general"
    max_result_chars: float = 100_000
```

### SideEffectLevel

```python
class SideEffectLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
# 只读通过 ToolMeta.is_readonly: bool 表示，不是第四个枚举值
```

---

## 执行模式

```python
from prodagent import ExecutionMode

ExecutionMode.REACTIVE      # 边走边想（默认）
ExecutionMode.PLAN_FIRST    # 先规划 DAG 后执行
# 没有 ExecutionMode.WORKFLOW——Workflow 通过 workflow= 参数传入
```

### Workflow

```python
from prodagent.plan import Workflow

wf = Workflow()

@wf.step
async def fetch_data() -> str:
    return "data"

@wf.step(depends_on=["fetch_data"])
async def analyze(fetch_data: str) -> str:
    return f"analyzed: {fetch_data}"

wf.llm_step(
    name="summarize",
    prompt="总结：{{analyze.output}}",
    depends_on=["analyze"],
    is_terminal=True,
)

wf.tool_step(
    name="save",
    tool_name="write_file",
    params={"path": "out.md", "content": "{{summarize.output}}"},
    depends_on=["summarize"],
)

plan = wf.compile()  # 编译为 Plan
```

---

## 预算

```python
from prodagent import HardBudget
from prodagent.kernel.budget import BudgetLedger

budget = HardBudget(
    max_turns=20,
    max_seconds=120.0,
    max_tokens=100_000,
    max_cost_usd=1.0,
)

# 多 Agent 共享账本
ledger = BudgetLedger(budget=budget)
await ledger.reserve(member="agent-a", cost_usd=0.3)
await ledger.commit(member="agent-a", cost_usd=0.25, reserved_cost_usd=0.3)
```

---

## 模型配置

```python
from prodagent.llm import LLMConfig
from prodagent.kernel.types import LLMResponse, StopReason, ToolCall
from prodagent.llm.fake import FakeLLMAdapter, script, RoutingFakeLLM

# OpenAI 兼容端点
llm_config = LLMConfig(model="deepseek-chat", temperature=0.0)

# FakeLLM（离线测试）
fake = FakeLLMAdapter(responses=[
    LLMResponse(
        content="",
        tool_calls=[ToolCall(name="search", params={"query": "x"})],
        stop_reason=StopReason.TOOL_USE,
    ),
    LLMResponse(content="答案", stop_reason=StopReason.END_TURN),
])

# script() 简写
fake = script(
    {"tool": "search", "params": {"query": "x"}},
    {"content": "答案"},
)

# 多 Agent 路由
routing = RoutingFakeLLM()
routing.add("agent-a", [LLMResponse(content="A 的回答")])
routing.add("agent-b", [LLMResponse(content="B 的回答")])
```

**环境变量**：
- `USE_FAKE_LLM=1` — 使用 FakeLLM
- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` — OpenAI 兼容端点
- `ANTHROPIC_API_KEY` — Anthropic 原生

---

## 框架配置

```python
from prodagent.base.config import FrameworkConfig, BackendConfig, bare, production

# 裸核（测试/CLI）
config = FrameworkConfig(backend=bare())

# 生产全套
config = FrameworkConfig(backend=production())

# 自定义后端
config = FrameworkConfig(backend=BackendConfig(
    checkpoint="postgres",   # file / postgres
    cache="redis",           # memory / redis
    dead_letter="redis",     # memory / redis
    graph="neo4j",           # file / neo4j
    # document/event_log/span/session: file / postgres
    # experience: 仅 file
    # approval: 仅 memory
    # lock: memory / redis
))
```

---

## 多 Agent 协作

```python
from prodagent import AgentConfig
from prodagent.coordination.ensemble import (
    EnsembleSpec, ensemble_stream, AgentFloorMember, RoundRobin,
)
from prodagent.coordination.blackboard import (
    BlackboardSpec, blackboard_stream, AgentBlackboardMember, Trigger,
)
from prodagent.coordination.work_queue import (
    WorkQueueSpec, work_queue_stream, WorkItem,
)

# ① Spawn（通过 agents= 配置）
parent = Agent("manager", config=AgentConfig(
    name="manager", agents=[child_a, child_b],
))

# ② Peer（通过 peers= 配置）
first = Agent("researcher", config=AgentConfig(
    name="researcher", peers=[writer, reviewer],
))

# ③ Ensemble
spec = EnsembleSpec(
    members=[AgentFloorMember(a, session_id="s1") for a in agents],
    topic="讨论话题",
    order=RoundRobin(),
)
async for event in ensemble_stream(spec):
    ...

# ④ Blackboard
spec = BlackboardSpec(
    experts={"researcher": AgentBlackboardMember(a, write_key="research")},
    triggers={"kickoff": Trigger(name="kickoff", keys=[], experts=["researcher"])},
)
async for event in blackboard_stream(spec):
    ...

# ⑤ WorkQueue
spec = WorkQueueSpec(
    workers={"w1": worker1, "w2": worker2},
    items=[WorkItem(item_id="1", payload="任务")],
)
async for event in work_queue_stream(spec):
    ...
```

## RunnerPort

```python
from prodagent.ports.runner import AgentActivation, InProcessChatRunner, InProcessRunner
from prodagent.runtime.parent_runtime import ParentRuntime

# 成员的一次发言（会话轮次，本地默认实现）
runner = InProcessChatRunner()
async for event in runner.activate(
    AgentActivation(agent=member, task="说说你的看法", session_id="floor-1")
):
    ...  # 终态事件携带 AgentRun

# spawn 形态的子执行（绑上本跳的 hooks / checkpoint / 账本）
runner = InProcessRunner(ParentRuntime(parent_run_id="root", llm=client))
async for event in runner.activate(
    AgentActivation(agent=child, task="子任务", run_id="root::child", parent_run_id="root")
):
    ...
```

`session_id` 有值是成员会话轮次，没有就是按 run_id 执行的正式 run；`InProcessRunner` 绑了 `ParentRuntime`，子执行按本跳接线 fork。换分布式，换端口实现即可，协作层不变。


---

## 事件类型

```python
from prodagent.kernel.types import (
    RunCompletedEvent,
    RunFailedEvent,
    RunSuspendedEvent,
    ThinkTokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
```

`agent.stream()` 产出这些事件的异步生成器。

---

## 异常体系

```python
from prodagent.base.errors import (
    AgentError,                # 所有框架异常的基类
    BudgetExceeded,            # 预算耗尽
    InfiniteLoopDetected,      # 死循环检测
    ToolAbortError,            # 工具不可重试错误
    ToolBlockedError,          # 工具被权限拦截
    PermissionDenied,          # 权限拒绝
    PromptInjectionDetected,   # 提示注入检测
    SensitiveContentDetected,  # 敏感内容检测
    SuspendPendingApproval,    # 审批挂起（内部控制流，不是错误）
    SecurityViolation,         # 安全违规
    VersionConflict,           # checkpoint 乐观并发冲突
    CorruptedCheckpointError,  # checkpoint 损坏
    UnknownApprovalError,      # 未知审批请求
    PlanAlreadyCompletedError, # 计划已完成
    RunIdCollisionError,       # run_id 冲突
)
```

---

## 模块索引

| 模块 | 内容 |
|------|------|
| `prodagent` | Agent, AgentConfig, ExecutionMode, HardBudget, tool 等顶层导出 |
| `prodagent.runtime.agent` | Agent 类 |
| `prodagent.runtime.config` | AgentConfig |
| `prodagent.kernel.loop` | ReactiveLoop |
| `prodagent.kernel.step` | Step |
| `prodagent.kernel.budget` | HardBudget / BudgetLedger |
| `prodagent.kernel.bus` | HookEvent / Gate / HookRegistry |
| `prodagent.kernel.types` | LLMResponse / ToolCall / ToolMeta / StopReason / 事件类型 |
| `prodagent.tooling.decorator` | @tool 装饰器 |
| `prodagent.tooling.base` | FunctionTool |
| `prodagent.tooling.dispatcher` | ToolDispatcher |
| `prodagent.plan.workflow` | Workflow |
| `prodagent.ports.*` | 所有端口定义 |
| `prodagent.coordination.*` | 多 Agent 协作原语 |
| `prodagent.cognition.*` | 上下文压缩与记忆 |
| `prodagent.hooks.*` | 审批、安全、可观测 |
| `prodagent.llm.*` | LLM 适配器、FakeLLM、定价 |
| `prodagent.base.config` | FrameworkConfig / BackendConfig / bare / production |

---

## 回到

- [学习路线首页 →](tour/index.md)
- [设计取舍 →](decisions.md)
- [术语表 →](glossary.md)
