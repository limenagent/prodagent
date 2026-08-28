# 架构全景：prodagent 是怎么搭起来的

> 这篇文档带你从上帝视角看 prodagent 的架构。读完你能在白板上画出整个运行时，说清每一层为什么存在、去掉会怎样、替代方案是什么。
>
> 适合：想理解 Agent 系统设计的开发者、想把 prodagent 用到自己项目的工程师、面试前复习架构的候选人。

---

## 一、整体架构：七层叠罗汉

prodagent 的代码不是平铺的，而是严格分层的。每一层只依赖它下面的层，上面的层可以换、可以拆、可以不用。

| 层 | 包 | 职责                                                                                            |
|:--:|:----|:------------------------------------------------------------------------------------------------|
| 7 | `playground` | 叶子节点——可视化调试 UI · FastAPI · 被 import-linter 隔离                                       |
| 6 | `hooks` · `skills` · `mcp` · `backends` | 横切能力——审批/权限/观测/审计 · runbook 蒸馏 · 外部工具桥接 · 5 种存储实现                      |
| 5 | `cognition` · `coordination` | 认知与协调——上下文压缩 + 四通道记忆；5 种协作原语 + 统一消息平面                                |
| 4 | `plan` · `tooling` · `llm` | 能力——DAG 规划与执行 · 工具系统 · 模型适配                                                      |
| 3 | `runtime` | 运行时——Agent 装配 · Factory · RunnerPort 的进程内实现（RunLoop）                               |
| 2 | `kernel` | 内核（纯逻辑，不依赖任何 capability）——types · state · budget · bus · step · loop · progress    |
| 1 | `ports` | 端口（Protocol 抽象，六边形架构的“左”侧）——17 个 Protocol + AgentSpec / 事件编解码 / Activation |
| 0 | `base` | 基础——配置 · 错误分类 · 事件日志 · 会话 · 重试 · 文本 · 时间                                    |

> 依赖方向自上而下——每一层只依赖它下面的层。

### 四条不可逾越的红线

这不是建议，是被 CI 强制执行的架构约束：

1. **kernel 不依赖任何 capability 包**——`kernel/` 的 import 只能指向 `base/` 和 `ports/`。它不知道 LLM 是 OpenAI 还是 Anthropic，不知道工具是函数还是 MCP，不知道存储是文件还是 Postgres。它只知道 Protocol。
2. **ports 是唯一的接缝**——所有跨层交互都通过 Protocol。换后端 = 换实现类，不动一行业务代码。没有任何模块直接 `import` 一个具体的后端实现。
3. **协作层不 import runtime**——`coordination/` 里没有任何一行执行 `import prodagent.runtime`，两个方向都有 CI 测试。执行 agent 走 `RunnerPort`，合并工具走 `tooling`，子 agent 名册从 `AgentSpec` 投影生成。协作原语不依赖进程内实现，分布式执行时换端口实现，协作层一行不改。
4. **playground 是叶子节点**——`import-linter` 强制检查：没有任何 prodagent 内部模块可以 `import playground`。核心永远不依赖 UI。

> **为什么要这么严格？** 因为架构腐化都是从"我就跨层 import 一次"开始的。prodagent 用 CI 把架构约束变成了不可违反的法律，而不是靠人自觉。

### 架构即定理

一条架构约束有三个可以栖身的地方：文档里（希望被遵守）、评审里（偶尔被拦住）、CI 里（不可能被违反）。prodagent 的选择是把依赖规则全部下放到第三层，并且逐级收紧——

| 测试 | 断言 | 层级 |
|------|------|------|
| `test_layering_contract` | 两两禁令：哪个包不得 import 哪个包（AST 扫描模块级 import） | 规则 |
| `test_kernel_purity` | kernel 的 import 闭包永远不含 capability 包 | 闭包 |
| `test_import_weight` | `import prodagent` 的重量不随时间膨胀 | 度量 |
| `test_no_import_cycles` | 全库 import 图拓扑排序无环 | **定理** |

最后一条值得多说一句。两两禁令有一个盲区：**每条边都合法，拼起来却可能成环**（a→b→c→a）——而环意味着不存在自底向上的阅读顺序，也不存在可以单独加载的模块。禁令约束的是局部，定理保证的是全局；从「这对包之间不许」到「整张图必须无环」，依赖规则才从纪律变成了数学事实。

同一个思想在数据侧的对应物是不变式：序列化往返、事件重放等价、账本会计恒等，不是被例子抽查，而是作为定律接受任意输入的检验（见 [测试与评估专题 →](topics/evaluation.md)）。**凡是能交给机器判定的约束，就不留给人的自觉**——这是贯穿本仓库的一条元原则。

---

## 二、六边形架构：17 个端口，5 种实现

prodagent 的核心架构模式是**六边形架构（Hexagonal Architecture / Ports & Adapters）**。

### 什么是六边形架构？

传统的分层架构是"上层调用下层"，业务逻辑依赖具体的数据库、具体的 API。六边形架构反过来：

- **业务逻辑在中心**，它只定义"我需要什么能力"（Port = Protocol）
- **具体实现在外面**（Adapter = 后端实现），它们来满足这些 Protocol
- 业务逻辑不知道也不关心外面是 Postgres 还是 MySQL，是 OpenAI 还是本地模型

```
        ┌─────────────────────────────┐
        │      业务逻辑（kernel）       │
        │  只知道 Protocol，不知道实现   │
        └──────────┬──────────────────┘
                   │ 依赖 Protocol
     ┌─────────────┼─────────────┐
     ▼             ▼             ▼
┌─────────┐  ┌─────────┐  ┌─────────┐
│ file    │  │ postgres│  │ memory  │  ← Adapter（实现 Protocol）
│ backend │  │ backend │  │ backend │
└─────────┘  └─────────┘  └─────────┘
```

### 17 个端口全景

prodagent 定义了 14 个 Protocol 端口，每一个都是一个"我需要什么能力"的契约：

| 端口 | 职责 | 现有实现 | 你可以替换成 |
|------|------|---------|------------|
| `LLMClient` | 模型调用（流式/结构化/缓存） | OpenAI / Anthropic / Fake | 任何有 `async complete()` 的客户端 |
| `RunnerPort` | 激活一个 agent 执行一次 run（spawn 子任务、舞台成员发言） | RunLoop（进程内） | 分布式 runtime / 远端 worker |
| `Tool` | 工具执行 | FunctionTool / MCP工具 | 你的自定义工具类 |
| `CheckpointStore` | 运行状态快照（崩溃恢复） | file / postgres / memory | MySQL / DynamoDB |
| `EventLog` | 事件追加日志（PLAN_FIRST状态） | file / postgres / memory | Kafka / Kinesis |
| `SessionStore` | 多轮会话存储 | file / postgres | Redis / 你的会话服务 |
| `CacheStore` | LLM 响应缓存 | memory / redis | Memcached / CDN |
| `LockStore` | 分布式锁（buzz_in仲裁） | memory / redis | etcd / ZooKeeper |
| `ApprovalStore` | HITL 审批请求存储 | memory | 你的审批系统 |
| `DeadLetterStore` | 死信队列（失败消息） | memory / redis | RabbitMQ / SQS |
| `DocumentStore` | 记忆文档存储 | file | MongoDB / Elasticsearch |
| `GraphStore` | 事实图谱存储 | file / neo4j | Neptune / TigerGraph |
| `SpanExporter` | 链路追踪导出 | file / postgres | OpenTelemetry / Jaeger |
| `Transport` | 多Agent消息传输 | in-process | NATS / gRPC / Kafka |
| `ExperienceStore` | 经验/技能存储 | file | 你的知识库 |

### 后端选型的哲学：每种数据去它该去的地方

`BackendConfig` 不是"一个数据库存所有东西"，而是按数据特性选引擎：

```python
@dataclass
class BackendConfig:
    # 关系型/持久化状态 → file（单机）或 postgres（多副本）
    document: Literal["file", "postgres"] = "file"
    checkpoint: Literal["file", "postgres"] = "file"
    event_log: Literal["file", "postgres"] = "file"
    span: Literal["file", "postgres"] = "file"
    session: Literal["file", "postgres"] = "file"

    # 临时/在途状态 → memory（单机）或 redis（多副本）
    cache: Literal["memory", "redis"] = "memory"
    lock: Literal["memory", "redis"] = "memory"
    approval: Literal["memory"] = "memory"
    dead_letter: Literal["memory", "redis"] = "memory"

    # 图数据 → 图数据库，没有替代
    graph: Literal["file", "neo4j"] = "file"
```

> **为什么不一个 Postgres 存所有？** 因为图查询在关系型数据库里是灾难（递归 JOIN 慢到无法接受），缓存用 Postgres 是浪费（你不需要持久化缓存），锁用 Postgres 是反模式（你需要的是 TTL + 原子操作，不是事务）。每种数据有它最适合的引擎，prodagent 不强行统一。

### 一致性测试：每个后端都必须通过同一套测试

`tests/backends/conformance/` 下有一套**后端一致性测试套件**。每新增一个后端实现，必须通过这套测试：

```
tests/backends/conformance/
├── approval.py       # ApprovalStore 必须通过的测试
├── cache.py          # CacheStore 必须通过的测试
├── checkpoint.py     # CheckpointStore 必须通过的测试
├── dead_letter.py    # DeadLetterStore 必须通过的测试
├── document.py       # DocumentStore 必须通过的测试
├── event_log.py      # EventLog 必须通过的测试
├── graph.py          # GraphStore 必须通过的测试
├── lock.py           # LockStore 必须通过的测试
└── span.py           # SpanExporter 必须通过的测试
```

然后每个后端有一个 `test_conformance_xxx.py` 来运行这套测试：

```python
# tests/backends/test_conformance_postgres.py
# 用 Postgres 后端跑完整套一致性测试
```

> **这就是"契约测试"的力量。** Protocol 定义了契约，一致性测试验证每个实现都遵守契约。你可以放心地把 file 后端换成 postgres，行为完全一致。

---

## 三、纯内核：kernel 为什么是"纯"的

`kernel/` 是整个框架的心脏。它的"纯"体现在三个层面：

### 1. 纯逻辑：不做任何 IO

kernel 里的代码不读文件、不发网络请求、不访问数据库。所有 IO 都通过 Protocol 端口注入：

```python
# kernel/step.py — Step 类的构造函数
class Step:
    def __init__(
        self,
        llm: LLMClient,        # ← Protocol，不是具体实现
        runner: ToolRunner,    # ← Protocol
        *,
        budget: HardBudget,    # ← 纯数据
        guard: ProgressGuard,  # ← Protocol
        bus: HookRegistry,     # ← 纯逻辑事件总线
        assembler: ContextAssembler,  # ← Protocol
        ...
    ):
```

Step 不知道 `llm` 是 OpenAI 还是 Fake，不知道 `runner` 执行的是本地函数还是 MCP 远程工具。它只知道"调用 `llm.complete()` 会返回一个 `LLMResponse`"。

### 2. 纯可测：不需要 mock 任何外部服务

因为 kernel 不做 IO，测试它不需要 mock 数据库、不需要 mock API：

```python
# 测试 Step：传入 FakeLLM 和 FakeToolRunner，断言行为
def test_step_stops_on_end_turn():
    llm = FakeLLM(responses=[LLMResponse(content="done", stop_reason=StopReason.END_TURN)])
    runner = FakeToolRunner()
    step = Step(llm, runner, budget=HardBudget(), ...)
    events = list(step.run(run, system="", tools=None))
    assert run.state == RunState.COMPLETED
```

> 对比：LangChain 的核心循环测试需要 mock OpenAI API、mock 向量数据库、mock 工具。prodagent 的 kernel 测试只需要构造 Python 对象。

### 3. 纯可替换：你可以换掉整个 kernel

因为 kernel 通过 Protocol 和外面交互，你可以写一个完全不同的循环实现（比如一个基于状态机的循环、一个基于 BFS 的规划循环），只要它满足 `LeafExecutor` Protocol，就能无缝接入 runtime。

### kernel 的七个模块，一个概念一个

```
kernel/
├── types.py      # 词汇：AgentRun 里流动的所有数据类型
│                  # (ToolCall, LLMResponse, ToolResult, AgentEvent...)
├── state.py      # 状态：AgentRun — 一次运行的可变状态对象
│                  # (messages, metrics, pending_handoff, checkpoint_version...)
├── budget.py     # 天花板：HardBudget（四轴）+ BudgetLedger（共享账本）
│                  # + check_budget（无状态检查函数）
├── bus.py        # 接缝：HookRegistry — 三协议总线
│                  # (fire 观察 / check 拦截 / collect 注入)
├── step.py       # 原子：Step — 一次 think→decide→execute
│                  # (assemble → call_llm → account → act)
├── loop.py       # 策略：ReactiveLoop — 迭代 Step 的策略
│                  # (何时停、恢复什么、怎么结算、怎么 checkpoint)
└── progress.py   # 守护：ProgressMonitor — 死循环检测
                   # (fingerprint 窗口 + 重复/停滞阈值)
```

> **注意模块之间的依赖方向：** types → state → budget → bus → step → loop → progress。每个模块只依赖它前面的模块。这不是巧合，是刻意设计的——你可以从 types 开始读，读到 loop 时所有依赖都已经理解了。

---

## 四、三协议总线：一个接缝连接所有横切关注点

`HookRegistry` 是 prodagent 最精妙的设计之一。它解决了一个经典问题：**横切关注点（审批、可观测、记忆、审计）怎么在不污染业务逻辑的前提下接入？**

### 传统做法：中间件链

很多框架用中间件链（middleware chain）：每个中间件包裹下一个，形成洋葱模型。问题是：

- 中间件之间有顺序依赖（审批必须在可观测之前？）
- 中间件可以修改请求/响应，调试困难
- 加一个新关注点要改链的结构

### prodagent 的做法：三协议总线

prodagent 把横切关注点分成三种**语义完全不同**的协议，挂载在同一个总线上：

| 维度 | `fire`（观察） | `check`（拦截） | `collect`（注入） |
|------|---------------|-----------------|-------------------|
| 语义 | 事件通知——“发生了一件事，你们随便看” | 门禁检查——“能不能做，你们说了算” | 内容注入——“有什么要加进来，都给我” |
| 执行方式 | 并发扇出 | 串行执行 | 并发收集 |
| 失败处理 | 只记录日志，不影响主流程 | 第一个否决即停，默认 fail-closed | 降级为 `None`，不影响其他注入器 |
| 返回值 | 无 | `BlockingResult` | `list[Any]` |

### 协议一：fire（观察）—— "发生了一件事，你们随便看"

```python
# 语义：事件通知。并发扇出，所有观察者同时收到。
# 失败：只记录日志，不影响主流程。
# 返回：无。

await bus.fire(HookEvent.TOOL_CALL, name="search", params={...})
await bus.fire(HookEvent.TOKEN_UPDATE, input_tokens=100, output_tokens=50, ...)
```

**典型观察者：**
- ConsoleObserver — 把事件打印到控制台
- SpanExporter — 把事件导出为 OpenTelemetry span
- CacheMonitor — 监控缓存命中率
- AuditLogger — 审计日志

> **为什么 fire 是并发的？** 因为观察者之间没有依赖，并发执行最快。而且观察者不应该影响主流程——如果一个观察者慢了或挂了，主流程不应该等它。

### 协议二：check（拦截）—— "这件事能不能做？你们说了算"

```python
# 语义：门禁检查。串行执行，第一个返回 blocked 的就停止。
# 失败：fail-closed（默认拒绝）—— 检查器挂了 = 拒绝，安全第一。
# 返回：BlockingResult（blocked=True/False + reason）

result = await bus.check_blocking(Gate.TOOL_CALL, tool_name="delete_file", params={...})
if result.blocked:
    return ToolResult.blocked_by(reason=result.reason)
```

**典型检查器：**
- ApprovalGate — HIGH 副作用工具需要人工审批
- RBACPolicy — 基于角色的权限控制
- PromptInjectionDetector — 提示注入检测
- OutputFilter — 输出内容过滤

> **为什么 check 是串行的？** 因为检查器之间有优先级（高优先级的先检查，被拒了就不用查低优先级的）。而且串行保证了"第一个否决就停止"的语义——如果并发，你不知道哪个否决先到。

> **为什么默认 fail-closed？** 因为安全系统的第一原则是"不确定就拒绝"。如果权限检查器因为数据库连接超时而挂了，你是放行还是拒绝？prodagent 选择拒绝——因为放行可能导致越权操作，拒绝最多是用户体验差。

### 协议三：collect（注入）—— "你们有什么要加进来的？都给我"

```python
# 语义：内容注入。并发执行，收集所有非 None 的结果。
# 失败：降级为 None，不影响其他注入器。
# 返回：list[Any] — 所有注入器的结果

memory_snippets = await bus.collect(InjectionPoint.CONTEXT_INJECTOR, query="...")
# memory_snippets 是所有注入器返回的文本片段列表
```

**典型注入器：**
- MemoryManager — 召回相关记忆
- SkillRegistry — 注入已调用技能的 runbook
- FloorViewInjector — 注入多 Agent 共享 floor 的对话记录

> **为什么 collect 是并发的？** 因为注入器之间没有依赖（记忆召回和技能注入互不影响），并发执行最快。而且一个注入器失败不应该影响其他的——记忆召回挂了，技能注入还能正常工作。

### 总线在循环中的接入点

循环（Step）在关键节点调用总线，自己完全不知道谁在监听：

```
Step.run()
  │
  ├── _prepare()
  │     ├── bus.fire(TURN_START)         ← 观察者：记录回合开始
  │     ├── assembler(run)                ← 内部调用 bus.collect(CONTEXT_INJECTOR)
  │     └── bus.fire(LLM_REQUEST)         ← 观察者：记录请求
  │
  ├── _call_llm()
  │     └── bus.fire(THINK)               ← 观察者：流式思维链
  │
  ├── _account()
  │     └── bus.fire(TOKEN_UPDATE)        ← 观察者：更新 token 计数
  │
  └── _runner.run_batch()
        ├── bus.check_blocking(TOOL_CALL) ← 拦截器：权限/审批
        ├── bus.fire(TOOL_CALL)            ← 观察者：记录调用
        └── bus.fire(TOOL_RESULT)          ← 观察者：记录结果
```

> **这就是"控制反转"的力量。** 循环只说"我要调用工具了"，它不知道也不关心有 3 个观察者在记录、2 个检查器在审批、1 个注入器在加记忆。加一个新的横切关注点 = 挂载一个 handler，不改循环一行代码。

---

## 五、数据流：一次 chat() 调用的数据旅程

让我们跟踪一次 `agent.chat("任务")` 调用，看数据怎么在各层之间流动。

### 阶段 1：入口（runtime）

```python
agent.chat("任务")
  → chat_stream()
    → _begin_chat_turn()     # 创建 ConversationSession，分配 run_id
    → drive_stream()         # 进入运行时驱动
```

**数据产生：**
- `ConversationSession` — 多轮会话状态（session_id, turns, version）
- `run_id` — 本次运行的唯一标识

### 阶段 2：装配（runtime/factory）

```
drive_stream()
  → LeafExecutorFactory.prepare()
    → agent.attach_default_hooks()   # 挂载 hooks（审批/观测/记忆）
    → agent.resolve_tools()          # 合并内联工具 + 注册表工具 + MCP工具
    → build_system_prompt()          # 组装系统提示
    → build_context_manager()        # 构建上下文管理器（如果开启压缩）
    → 选择执行模式：
        REACTIVE   → ReactiveLoop
        PLAN_FIRST → PlanExecutor
        Workflow   → 静态 DAG 执行器
```

**数据产生：**
- `HookRegistry` — 三协议总线，挂载了所有横切关注点
- `ToolDispatcher` — 工具调度器，持有工具映射
- `ContextManager` — 上下文管理器（可选）
- `ReactiveLoop` / `PlanExecutor` — 执行器

### 组装根：一切装配的唯一起点

依赖注入走到尽头，是一个不显眼但决定性的结论：**对象图必须在唯一一处组装。** prodagent 把这个位置命名为 `runtime/compose.py`——全仓库只有这一个文件读 `profile`（bare 还是 production），也只有它回答「一个生产级 agent 由什么构成」。

为什么唯一性如此重要？因为**装配散落等于配置知识散落**。一旦有两处代码都能决定「生产模式要不要挂缓存」，它们迟早给出不同答案——在不同时机、以不同组合给出，而这类偏差不报错，只表现为「测试环境和生产环境行为不一致」，等你用一整个下午去猜。组装根把「什么构成生产」从散布的 if 变成一处的清单。

compose 的 docstring 同时列出了能力的全部三个插座：**端口替换**（实现一个 Protocol）、**总线挂载**（在 HookRegistry 上注册）、**执行器替换**（实现 LeafExecutor）。这份清单的价值不在列举了什么，而在宣告了边界——新能力必然落进三者之一，没有第四种暗门。**一个框架的可扩展性等价于它的插座清单：插座越少越明确，暗门越少越可信。**

### 阶段 3：循环（kernel）

```
ReactiveLoop.stream()
  → _resolve_run()           # 新建或从 checkpoint 恢复 AgentRun
  → while True:
      → Step.run()
        → _prepare()         # 预算检查 + 死循环检测 + 上下文组装
        → _call_llm()        # 硬超时 + 流式 + 缓存边界
        → _account()         # token/cost 记账 + 消息追加
        → _runner.run_batch() # 工具调度（只读并行/写串行）
      → _record_turn()       # 事件日志 + checkpoint
      → 检查终止条件
```

**数据流动：**
- `AgentRun` — 贯穿整个循环的可变状态对象
  - `messages` — 对话历史（被 ContextManager 压缩/替换）
  - `metrics` — token/cost/turn 计数
  - `pending_tool_call` / `pending_handoff` — 恢复点
  - `checkpoint_version` — 乐观并发版本号
- `LLMResponse` — 模型响应（content, tool_calls, stop_reason, tokens）
- `ToolResult` — 工具执行结果（outcome, value, error）

### 阶段 4：工具执行（tooling）

```
ToolDispatcher.run_batch()
  → 分类：只读工具 → 并行队列；写工具 → 串行队列
  → 对每个调用：
    → bus.check_blocking(TOOL_CALL)   # 权限/审批检查
    → dispatch_with_retry()            # 重试 + 熔断器
    → 执行工具函数
    → build_tool_message()             # 结果写回 transcript（可能 spill）
```

**数据产生：**
- `ToolCall` — 模型请求的工具调用（name, params, call_id）
- `ToolResult` — 执行结果（OK/RETRY/ABORT/BLOCKED/SUSPENDED/HANDOFF）
- 工具结果消息 — 追加到 `AgentRun.messages`

### 阶段 5：横切关注点（hooks）

在整个流程中，hooks 通过总线被调用：

```
fire(TURN_START)      → ConsoleObserver 打印
fire(LLM_REQUEST)     → SpanExporter 开始 span
fire(THINK)           → ConsoleObserver 流式输出
fire(TOKEN_UPDATE)    → CacheMonitor 更新统计
check(TOOL_CALL)      → ApprovalGate 检查是否需要审批
collect(CONTEXT_INJECTOR) → MemoryManager 召回记忆
fire(TOOL_RESULT)     → AuditLogger 记录审计
```

### 阶段 6：终止与返回

```
循环终止条件（任一满足）：
  - 模型返回 END_TURN（没有工具调用）→ COMPLETED
  - 预算触顶 → FAILED
  - 死循环检测 → FAILED
  - HITL 审批挂起 → SUSPENDED
  - 多 Agent handoff → COMPLETED（控制权转移）

→ session.complete_turn()   # 保存会话状态
→ store.save(session)        # 持久化
→ 返回 AgentRun（含 final_output）
```

---

## 六、控制流：三种执行模式的选择

prodagent 支持三种执行模式，按任务复杂度选择：

### REACTIVE（反应式）—— "想到什么做什么"

```
用户输入
  → 模型思考 → 决定调用工具 → 执行工具 → 模型思考 → ...
  → 模型给出最终答案
```

- **适用场景：** 简单问答、单轮工具调用、对话式交互
- **特点：** 没有预先规划，每一步模型自己决定做什么
- **实现：** `ReactiveLoop` — 迭代 `Step`，直到模型返回 END_TURN

### PLAN_FIRST（先规划后执行）—— "先列计划再干活"

```
用户输入
  → Planner（一次LLM调用）生成 DAG 计划
  → PlanExecutor 按 DAG 拓扑顺序执行步骤
  → 步骤失败 → Replan（增量重规划）
  → 所有步骤完成 → 汇总结果
```

- **适用场景：** 复杂多步骤任务、需要并行执行的任务、需要断点续跑的任务
- **特点：** 先有计划 DAG，再按依赖关系执行；支持动态重规划
- **实现：** `Planner` + `PlanExecutor` + `Plan`（DAG）

### Workflow（工作流）—— "按固定流程走"

```
用户输入
  → 预定义的静态 DAG（开发者写死的步骤和依赖）
  → 按 DAG 执行
  → 完成
```

- **适用场景：** 固定流程的业务场景（审批流、数据处理管道）
- **特点：** 计划是静态的，不由 LLM 生成；确定性高
- **实现：** `Workflow` 类 — 编译成 `initial_plan`，用 PLAN_FIRST 执行器执行

### 怎么选？

```
任务简单？ → REACTIVE
任务复杂但流程不固定？ → PLAN_FIRST
任务流程固定且需要确定性？ → Workflow
```

> **prodagent 的哲学：** 不要一上来就用 PLAN_FIRST。很多"复杂任务"其实用 REACTIVE + 好的上下文管理就能搞定。PLAN_FIRST 有额外的规划开销（多一次 LLM 调用），而且规划本身可能出错。只有当任务确实需要多步骤并行、或者需要断点续跑时，才用 PLAN_FIRST。

---

## 七、关键抽象详解

### AgentRun：一次运行的"单一真相源"

`AgentRun` 是整个框架中最重要的数据结构。一次运行的所有可变状态都在这里：

```python
@dataclass
class AgentRun:
    # 标识
    run_id: str                    # 唯一标识
    task: str                      # 用户任务
    state: RunState                # RUNNING / COMPLETED / FAILED / SUSPENDED

    # 对话
    messages: MessageList          # 对话历史（被 ContextManager 管理）
    final_output: str | None       # 最终输出

    # 计量
    metrics: RunMetrics            # turn/token/cost 计数
    start_time: float              # 开始时间（用于 seconds 预算）

    # 工具
    tool_history: list[ToolCall]   # 工具调用历史（死循环检测用）
    tool_failures: int             # 失败计数
    retry_counter: dict[str, int]  # 每个工具的重试次数

    # 恢复点
    pending_tool_call: ToolCall | None     # 等待审批的工具调用
    pending_approval_id: str | None        # 审批请求 ID
    pending_handoff: PendingHandoff | None # 等待接力的 handoff

    # 持久化
    checkpoint_version: int        # 乐观并发版本号
    plan_state: JsonDict | None    # PLAN_FIRST 的 DAG 状态
    last_event_seq: int            # 事件日志尾序号
```

> **为什么所有状态都在一个对象里？** 因为 checkpoint 就是序列化这个对象。如果状态分散在各处，checkpoint 就要序列化多个对象，恢复时要组装，容易出错。单一真相源 = 序列化一个对象 = 恢复一个对象。

### Step：agency 的原子

`Step` 是"一次模型调用 + 最多一轮工具执行"的原子单元：

```
Step.run()
  │
  ├── _think()
  │     ├── _prepare()         # 预算检查 → 死循环检测 → 上下文组装 → fire(TURN_START)
  │     ├── _call_llm()        # 硬超时 → 流式回调 → 缓存边界
  │     └── _account()         # token/cost 记账 → 消息追加 → fire(TOKEN_UPDATE)
  │
  ├── 终止判断
  │     └── 如果 stop_reason == END_TURN → 标记 COMPLETED，返回
  │
  └── _runner.run_batch()      # 工具调度
        ├── 只读工具并行执行
        └── 写工具串行执行
```

> **Step 是原子的意思是：** 要么完整执行一次 think→decide→execute，要么不执行。不存在"执行了一半的 Step"。这使得 checkpoint 可以精确地在 Step 之间保存——恢复时从下一个 Step 开始，不会重复执行半个 Step。

### BudgetLedger：多 Agent 共享预算账本

当多个 Agent 并发运行时（spawn 子 Agent、ensemble 投票），它们需要共享一个预算上限。`BudgetLedger` 就是这个共享账本：

```
BudgetLedger
  ├── _committed    # 已结算的花费（永久，只增不减）
  ├── _reserved     # 已预留的花费（临时，commit 后清除）
  ├── _reserved_by  # 每个成员的预留明细
  └── _lock          # asyncio.Lock（保护并发修改）

操作：
  reserve(member, turns, tokens, cost)   # 预留预算（防止超卖）
  commit(member, turns, tokens, cost)     # 结算实际花费
  release(member, turns, tokens, cost)    # 释放未使用的预留
  check(member)                            # 检查是否超预算
```

> **为什么需要 reserve/commit 而不是直接 commit？** 因为并发场景下，如果两个子 Agent 同时开始执行，它们都不知道对方会花多少。如果不预留，可能两个都执行完了才发现总预算超了——钱已经花了。reserve 就是"先占座"：开始前预留预估预算，让其他成员看到这个预算已经被占用了；结束后用实际花费结算，多退少补。

---

## 八、扩展点地图：你可以在哪里扩展 prodagent

prodagent 的设计目标之一是"每个机制都能独立扩展"。以下是完整的扩展点地图：

### 扩展点 1：新的 LLM 提供商

**做什么：** 实现 `LLMClient` Protocol

```python
class MyLLMClient:
    async def complete(self, messages, *, system, tools, config, on_chunk):
        # 调用你的模型 API
        return LLMResponse(content=..., tool_calls=...)
```

**接入：** `AgentConfig(llm=MyLLMClient())`

### 扩展点 2：新的工具来源

**做什么：** 实现 `Tool` Protocol，或注册到 `ToolRegistry`

```python
@tool(name="my_tool", readonly=False, side_effect_level=SideEffectLevel.HIGH)
async def my_tool(param: str) -> str:
    ...
```

**接入：** `Agent(tools=[my_tool])` 或 `AgentConfig(tool_registry=MyRegistry())`

### 扩展点 3：新的存储后端

**做什么：** 实现对应的 Protocol（`CheckpointStore` / `EventLog` / ...），通过一致性测试

**接入：** `BackendConfig` 中指定，或在 `backends/registry.py` 注册

### 扩展点 4：新的横切关注点

**做什么：** 挂载到 `HookRegistry`

```python
# 观察者
hooks.register_event(HookEvent.TOOL_CALL, my_observer)

# 检查器
hooks.register_checker(Gate.TOOL_CALL, my_checker)

# 注入器
hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, my_injector)
```

**接入：** `AgentConfig(event_handlers=[...], checkers=[...], injectors=[...])`

### 扩展点 5：新的多 Agent 协作拓扑

**做什么：** 基于 `coordination/` 的原语组合，或实现新的 `StageDriver`

**现有原语：** spawn（委派）、peer（接力）、ensemble（投票）、blackboard（黑板）、work_queue（队列）

### 扩展点 6：新的执行模式

**做什么：** 实现 `LeafExecutor` Protocol

```python
class MyExecutor:
    async def stream(self, task, *, run_id, parent_run_id):
        # 你的执行逻辑
        yield AgentEvent(...)
```

**接入：** 通过 `runtime/factory.py` 的扩展点注册

### 扩展点 7：新的记忆通道

**做什么：** 实现记忆通道 Protocol，加入 `MemoryManager` 的通道列表

**现有通道：** RuleChannel（规则）、EntityChannel（实体）、ExactChannel（精确匹配）、SemanticChannel（语义搜索）

---

## 九、架构美感总结

prodagent 的架构之美在于几个"恰好"：

1. **恰好的分层**——7 层不多不少。太少会导致职责混杂，太多会导致理解成本爆炸。7 层是"一个人能在脑子里装下整个架构"的上限。

2. **恰好的抽象**——17 个端口。每个端口对应一个真实的、可替换的能力。没有为了"优雅"而过度抽象（比如没有把"序列化"单独抽成端口），也没有该抽象的地方不抽象（比如 LLM 调用直接耦合 OpenAI SDK）。

3. **恰好的纯度**——kernel 是纯的，但不是整个框架都是纯的。如果整个框架都是纯的，IO 就要到处传递，代码会变得啰嗦。prodagent 只把最核心的循环逻辑做成纯的，外面的层允许有状态——这是"纯度"和"可用性"的最佳平衡点。

4. **恰好的默认**——bare profile 零文件起步，production() 一键全套。不是"默认全关让你自己开"（太麻烦），也不是"默认全开"（太重）。默认够用，升级一键。

5. **恰好的测试**——1,182 个全离线测试。不是"测试覆盖 100%"（为了覆盖率写无意义的测试），也不是"只测 happy path"。每个机制都有对应的测试，测试是可复现的、确定性的、快速的。

> **架构不是设计出来的，是演化出来的。** prodagent 的架构经历了多次重构（kernel 从 runtime 拆出来、messaging 平面从各拓扑抽出来、三协议总线从事件系统演化出来）。每一次重构都是因为"当前架构不够用了"，而不是"为了重构而重构"。最终的架构是"恰好够用"的——不超前设计，也不落后于需求。

---

## 下一步

- 想深入理解一次调用的生命周期？→ [心智模型](mental-model.md)
- 想理解每个设计决策的"为什么"？→ [设计哲学](design-philosophy.md) / [设计取舍](decisions.md)
- 想跟着源码学？→ [第一部分 · 一次调用的生命周期](tour/index.md)
- 想把 prodagent 用到自己的项目？→ [5 分钟上手](start.md)
