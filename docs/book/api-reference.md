# 附录 · API 参考

> 核心 API 速查。详细接口签名和 docstring 请参考源码或运行 `mkdocs serve` 查看自动生成文档。

零基础读者先花一分钟知道这份参考怎么读。所谓 **API 参考**，就是框架对外提供的"零件清单"：每个类怎么构造、每个函数要传什么参数、返回什么。下面代码里以 `#` 开头的是注释，只给人看、程序不执行。**位置参数**指按顺序传入、不用写名字的参数；**关键字参数**指调用时写成 `名字=值` 的参数（比如 `run_id="r1"`），好处是一眼看得出每个值是给谁的。**docstring** 是写在函数开头第一行的三引号字符串，用来描述这个函数干什么，工具能自动把它们收集成文档。这份参考不需要通读，写代码时回来查对应小节即可。

---

## 核心入口

### Agent

```python
from prodagent import Agent, AgentConfig
agent = Agent(
    "demo",                                    # name（位置参数，必填）
    system_prompt="你是一个 helpful assistant",  # 系统提示
    tools=[search, fetch],                     # 可用工具
    budget=HardBudget(max_turns=20),           # 四轴预算
    workflow=wf,                               # 可选：手写 Workflow（绑定为预置图）
    config=AgentConfig(name="demo"),           # 完整配置（LLM、后端、hooks 等）
)
```

> 执行没有模式枚举：不绑 workflow 的 agent，对话就是它本体在跑（单节点
> 循环）；绑了 workflow 的 agent 跑一张预置图。形状是组合决定，不是开关。

**主要方法**：

| 方法 | 返回 | 说明 |
|------|------|------|
| `await chat(message, *, session_id=None, resume=False, as_unit=False)` | `Run` | 执行一次任务，返回最终 Run |
| `chat_stream(message, *, session_id=None, ...)` | `AsyncGenerator[AgentEvent]` | 流式执行，逐事件产出 |

> **注意**：Agent 构造函数没有 `llm` 参数。LLM 客户端通过 `AgentConfig(llm=...)` 传入。

### AgentConfig

```python
@dataclass
class AgentConfig:
    name: str
    system_prompt: str = ""
    tools: list[Tool] = field(default_factory=list)
    budget: HardBudget | None = None
    llm: LLMClient | None = None               # LLM 客户端
    framework: FrameworkConfig | None = None   # 框架配置（后端、压缩等）
    hooks: HookRegistry | None = None          # 事件总线
    agents: list[Agent] = field(default_factory=list)   # spawn 子 Agent
    peers: list[Agent] = field(default_factory=list)    # peer 接力
    memory: MemoryProvider | None = None       # 记忆系统
    skills: SkillRegistry | None = None        # 技能注册表
    initial_plan: Plan | None = None           # 预置图（绑 workflow= 时自动设置）
    registry: BodyRegistry | None = None       # 命名 body 名册
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
    timeout_seconds: float = 9.0
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

## 执行形状（无模式枚举）

```python
# 形状一：agent 本体（默认）——对话即循环，无规划
agent = Agent("demo", system_prompt="...")

# 形状二：预置图——手写 Workflow 绑定为固定流程
agent = Agent("demo", workflow=wf)
```

### Workflow

```python
from prodagent import Workflow
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
await ledger.reserve(member="agent-a", turns=1, cost_usd=0.3)
await ledger.commit(member="agent-a", turns=1, tokens=0, cost_usd=0.25, reserved_cost_usd=0.3)
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

**环境变量**：环境变量是操作系统里一组"名字=值"的配置，程序启动时读它来决定行为，好处是不用把密钥写进代码。本框架认这几个：

- `USE_FAKE_LLM=1` — 使用 FakeLLM
- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` — OpenAI 兼容端点
- `ANTHROPIC_API_KEY` — Anthropic 原生

---

## 框架配置

```python
from prodagent.base.config import FrameworkConfig, BackendConfig, production
# 裸核是默认档位（profile="bare"），不需要任何函数：
config = FrameworkConfig()          # 不落盘、不挂审批、不开缓存压缩
# 生产全套：production() 返回整套 FrameworkConfig（profile 切到 "production"）
config = production()
# 接到 Agent 上：
agent = Agent("demo", config=AgentConfig(name="demo", framework=production()))
# 自定义后端：在 FrameworkConfig.backend 上填 BackendConfig
config = FrameworkConfig(backend=BackendConfig(
    checkpoint="postgres",   # file / postgres
    cache="redis",           # memory / redis
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
from prodagent import Agent, AgentConfig
# ① Spawn（通过 agents= 配置）——委派，call-return：父保持控制权等结果
parent = Agent("manager", config=AgentConfig(
    name="manager", agents=[child_a, child_b],
))
# ② Peer（通过 peers= 配置）——接力，handoff：当前 run 结束，同伴的 run 接过链条
first = Agent("researcher", config=AgentConfig(
    name="researcher", peers=[writer, reviewer],
))
```

委派语义是显式的两种：spawn 是 call-return（结果回到调用方），peer 是 handoff（控制权真转移、不回来）。黑板形的协作（专家机会式写共享工作区）不是第三个原语——它由图原子（Route 的 selector 读完整 state + Loop）拼装，见 [ch09](ch09.md)。

---

## RunnerPort

```python
from prodagent.ports.execution import AgentActivation, InProcessChatRunner
from prodagent.runtime.runner import InProcessRunner
# 成员的一次发言（会话轮次，本地默认实现）
runner = InProcessChatRunner()
async for event in runner.activate(
    AgentActivation(agent=member, task="说说你的看法", session_id="floor-1")
):
    ...  # 终态事件携带 Run
# 子执行（绑上本跳的 RunContext，子 agent 按本跳接线 fork）
runner = InProcessRunner(ctx)
async for event in runner.activate(
    AgentActivation(agent=child, task="子任务", run_id="root::child", parent_run_id="root")
):
    ...
```

`session_id` 有值是成员会话轮次，没有就是按 run_id 执行的正式 run；`InProcessRunner` 绑了本跳的 `RunContext`，子执行按本跳接线 fork。换分布式，换端口实现即可，协作层不变。

---

## 事件类型

```python
from prodagent.kernel.types import (
    RunCompletedEvent,
    RunFailedEvent,
    RunSuspendedEvent,
    ThinkTokenEvent,
    ToolCallStartEvent,
    ToolResultEvent,
)
```

`agent.chat_stream()` 产出这些事件的异步生成器。

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

读源码时按这张表定位：一个**模块**就是一个 `.py` 文件或一个装着若干文件的目录（包），点号表示层级，比如 `prodagent.kernel.budget` 对应 `prodagent/kernel/budget.py`。

| 模块 | 内容 |
|------|------|
| `prodagent` | Agent, AgentConfig, HardBudget, tool 等顶层导出 |
| `prodagent.runtime.agent` | Agent 类 |
| `prodagent.runtime.config` | AgentConfig |
| `prodagent.runtime.recipes.agent_loop` | AgentLoop / Round |
| `prodagent.kernel.scheduler` | Scheduler |
| `prodagent.kernel.budget` | HardBudget / BudgetLedger |
| `prodagent.kernel.bus` | HookEvent / Gate / HookRegistry |
| `prodagent.kernel.types` | LLMResponse / ToolCall / ToolMeta / StopReason / 事件类型 |
| `prodagent.tooling.decorator` | @tool 装饰器 |
| `prodagent.tooling.base` | FunctionTool |
| `prodagent.tooling.dispatcher` | ToolDispatcher |
| `prodagent.kernel.workflow` | Workflow（声明式图构建器） |
| `prodagent.kernel.compiler` | @workflow 编译器 / compile |
| `prodagent.kernel.combinators` | Sequential / Parallel / Route / Loop |
| `prodagent.kernel.registry` | BodyRegistry |
| `prodagent.ports.*` | 所有端口定义 |
| `prodagent.runtime.compose` | 协作三件套（call / transfer / settle） |
| `prodagent.cognition.*` | 上下文压缩与记忆 |
| `prodagent.hooks.*` | 审批、安全、可观测 |
| `prodagent.llm.*` | LLM 适配器、FakeLLM、定价 |
| `prodagent.base.config` | FrameworkConfig / BackendConfig / production |

---

## 回到

→ [回到书首页](../index.md) · [附录速查](appendix.md)

- [附录 · 关键取舍速查 →](appendix.md)
- [附录 · 术语表 →](appendix.md)
