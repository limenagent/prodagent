# 设计哲学：prodagent 的 10 条核心原则

> 每一个框架都有它的"性格"。prodagent 的性格写在这 10 条原则里。
>
> 读完你不仅知道 prodagent 是怎么设计的，更知道**为什么**这么设计——以及在你自己的项目里，什么时候该遵循、什么时候该打破这些原则。

---

## 原则一：裸核默认，生产一键

> 默认够用，升级一键。不是"默认全关让你自己开"，也不是"默认全开"。

### 为什么

很多框架走两个极端：

- **极端 A：默认全开**——`Agent()` 一初始化就连数据库、起线程、开缓存。简单 demo 很重，生产环境想关某个功能要找半天配置。
- **极端 B：默认全关**——`Agent()` 什么都不做，你要手动组装 10 个组件才能跑起来。学习曲线陡峭，新手第一个 demo 就劝退。

prodagent 走中间：

```python
# 裸核：零文件、零旁路、零配置
agent = Agent("demo", tools=[search])
asyncio.run(agent.chat("你好"))

# 生产：一行切换全套护甲
from prodagent.base.config import production
agent = Agent("demo", tools=[search],
              config=AgentConfig(name="demo", framework=production()))
```

### bare profile 有什么、没什么

| 能力 | bare（默认） | production |
|------|-------------|------------|
| 核心循环 | ✅ | ✅ |
| 工具调度 | ✅ | ✅ |
| LLM 调用 | ✅ | ✅ |
| 内存会话 | ✅ | ✅ |
| 四轴预算 | ✅（安全网默认值） | ✅（可配置） |
| 死循环检测 | ✅ | ✅ |
| checkpoint 落盘 | ❌ | ✅ |
| 事件日志 | ❌ | ✅ |
| span 追踪 | ❌ | ✅ |
| HITL 审批门 | ❌ | ✅（HIGH 工具） |
| LLM 响应缓存 | ❌ | ✅ |
| 上下文压缩 | ❌ | ✅ |
| 工具结果 spill | ❌ | ✅ |

### 反例

LangChain 的 `AgentExecutor` 默认什么都有（重试、回调、内存），但你想关掉某个功能要翻半天文档。AutoGen 的 `AssistantAgent` 默认连配置都不全，新手跑第一个例子要配 5 个东西。

> **在你自己的项目里：** 问自己——"一个完全不了解我框架的人，复制粘贴 3 行代码能跑起来吗？"如果不能，你的默认太重了。再问——"生产环境的人，能一行代码切换到全套护甲吗？"如果不能，你的升级路径太复杂了。

---

## 原则二：纯内核——把"发动机"从"整车"里拆出来

> kernel 不依赖任何 capability 包。循环、预算、状态、总线都是纯逻辑。

### 为什么

想象你在造一辆车。如果发动机和方向盘、座椅、音响焊在一起，你会：

- 无法单独测试发动机（要组装整车才能测）
- 无法更换发动机（要拆整车）
- 无法理解发动机（要先理解整车）

Agent 框架也是一样。如果"循环逻辑"和"LLM 调用"、"工具执行"、"数据库存储"焊在一起，你会：

- 无法单独测试循环（要 mock 所有外部依赖）
- 无法更换循环策略（要改一大坨代码）
- 无法理解循环（要先理解整个框架）

prodagent 把 kernel 拆出来：

```
kernel/  ← 纯逻辑，只依赖 base/ 和 ports/
  types.py      # 数据结构
  state.py      # 运行状态
  budget.py     # 预算检查
  bus.py        # 事件总线
  step.py       # 一次 think→decide→execute
  loop.py       # 迭代策略
  progress.py   # 死循环检测
```

kernel 里的代码：

- 不 `import openai` 或 `import anthropic`
- 不 `import redis` 或 `import psycopg`
- 不读文件、不发网络请求
- 所有外部能力通过 Protocol 注入

### 这带来了什么

**1. 可独立测试**

```python
# 测试循环：不需要真实 LLM，不需要数据库
def test_loop_stops_on_budget():
    llm = FakeLLM(responses=[...])  # 纯 Python 对象
    runner = FakeToolRunner()        # 纯 Python 对象
    loop = ReactiveLoop(llm, runner, budget=HardBudget(max_turns=1))
    events = list(loop.stream("task"))
    assert any(isinstance(e, RunFailedEvent) for e in events)
```

**2. 可独立替换**

你可以写一个完全不同的循环实现（比如基于状态机的、基于 BFS 规划的），只要它满足 `LeafExecutor` Protocol，就能无缝接入 runtime。

**3. 可独立理解**

读 kernel 时，你不需要知道 LLM 是 OpenAI 还是 Anthropic，不需要知道工具是本地函数还是 MCP 远程调用。你只需要理解"循环逻辑"本身。

### 反例

LangChain 的 `AgentExecutor` 把循环逻辑和 LLM 调用、工具执行、回调系统混在一起。想理解循环怎么工作，要同时理解 5 个子系统。想换一种循环策略，要 fork 整个包。

> **在你自己的项目里：** 找到你的"发动机"——那个最核心、最不应该和外部依赖耦合的逻辑。把它拆出来，让它只依赖抽象（Protocol/接口），不依赖具体实现。测试它的时候不需要 mock 任何外部服务。

---

## 原则三：三协议总线——一个接缝连接所有横切关注点

> fire（观察）、check（拦截）、collect（注入）。三种语义，一个总线。

### 为什么

横切关注点（审批、可观测、记忆、审计）是每个框架都头疼的问题。传统做法有几种：

**做法 A：中间件链**——每个中间件包裹下一个，形成洋葱模型。问题：中间件之间有顺序依赖，中间件可以修改请求/响应（调试困难），加新关注点要改链结构。

**做法 B：回调函数**——在关键位置注册回调。问题：回调只有一种语义（"发生了一件事"），但实际需要三种语义（"观察"、"拦截"、"注入"）。用回调做拦截要靠约定（返回 False 表示拦截），容易出错。

**做法 C：AOP（面向切面编程）**——通过注解/装饰器在方法前后插入逻辑。问题：调试困难（调用栈不直观），性能开销大，Python 生态支持不好。

prodagent 用三协议总线：

```
HookRegistry
  ├── fire(event, **data)     → 观察：并发扇出，失败只记录，不返回值
  ├── check_blocking(gate, **data) → 拦截：串行执行，第一个否决就停止，fail-closed
  └── collect(point, **data)  → 注入：并发收集，失败降级 None，返回结果列表
```

### 三种语义的本质区别

| 维度 | fire（观察） | check（拦截） | collect（注入） |
|------|-------------|-------------|----------------|
| 问题 | "发生了什么？" | "能不能做？" | "有什么要加的？" |
| 执行方式 | 并发 | 串行 | 并发 |
| 失败处理 | 记录日志，继续 | fail-closed（默认拒绝） | 降级为 None |
| 返回值 | 无 | BlockingResult | list[Any] |
| 典型用途 | 日志、追踪、监控 | 权限、审批、安全 | 记忆、技能、上下文 |
| 挂载点 | HookEvent（30+ 种） | Gate（10 种） | InjectionPoint（1 种） |

### 为什么 check 是 fail-closed 的

这是安全系统的第一原则：**不确定就拒绝**。

```python
# bus.py
def _handle_checker_failure(self, point_name, checker, exc):
    if self._failure_policy is FailurePolicy.FAIL_CLOSED:
        # 检查器挂了 = 拒绝
        return BlockingResult(blocked=True, reason=f"Checker failed: {exc}")
    # fail-open 是可选的，但默认不用
```

想象一个场景：权限检查器因为数据库连接超时而挂了。你是放行还是拒绝？

- 放行 → 可能导致越权操作（安全事故）
- 拒绝 → 用户体验差，但安全

prodagent 选择拒绝。因为安全事故的代价远大于用户体验差的代价。

### 为什么 fire 是并发的

因为观察者之间没有依赖。ConsoleObserver 打印日志和 SpanExporter 导出追踪互不影响，并发执行最快。而且观察者不应该影响主流程——如果一个观察者慢了或挂了，主流程不应该等它。

### 反例

OpenAI 的函数调用没有审批机制——模型说调什么工具就调什么。想加审批要自己在工具函数里写 `if 需要审批: 挂起`，每个工具都要写，容易遗漏。

AutoGen 的 `GroupChat` 用回调做可观测，但回调只有一种语义——想做审批要靠"回调返回特殊值"的约定，不类型安全，容易出错。

> **在你自己的项目里：** 找到你的"关键接缝"——那个所有横切关注点都要经过的地方。不要用一种回调机制应付所有场景，而是根据语义区分：观察用并发事件、拦截用串行门禁、注入用并发收集。

---

## 原则四：四轴预算互为兜底

> turns / seconds / tokens / cost，任一轴都可能被绕过，但四轴同时生效就很难失控。

### 为什么

很多框架只有 `max_iterations`（轮数限制）。这够吗？

- **20 轮可能只花 $0.01**（每轮短问答），也可能花 $50（每轮 100k token）
- **死循环时每轮可能只花很少钱**，但永远不停
- **API 可能很快返回但烧了很多 token**（比如模型生成了超长输出）

单一轴的预算是不够的。prodagent 用四轴：

```python
@dataclass
class HardBudget:
    max_turns: int = 20          # 轮数：防止死循环
    max_seconds: float = 120.0   # 时间：防止卡住
    max_tokens: int = 100_000    # token：防止上下文爆炸
    max_cost_usd: float = 1.0    # 成本：防止烧钱
```

### 四轴的检查时机

预算不是"最后算总账"，而是在关键节点实时检查：

```
Step.run()
  ├── _prepare()
  │     └── check_budget()    ← 每次思考前检查
  ├── _call_llm()
  │     └── asyncio.wait_for(timeout=remaining_seconds)  ← 硬超时
  ├── _account()              ← 记账
  └── _runner.run_batch()
        └── 每个工具执行后 check_budget()  ← 工具执行后检查
```

### 时间轴的特殊处理：硬超时，不是事后检查

```python
# 错误做法：调用完了再看超没超
response = await llm.complete(...)
if time_elapsed > max_seconds:
    raise BudgetExceeded()  # 钱已经花了，时间已经过了

# prodagent 的做法：到点直接掐断
llm_timeout = max(0.1, budget.max_seconds - run.elapsed_seconds())
response = await asyncio.wait_for(coro, timeout=llm_timeout)
```

事后检查时，损失已经发生了。硬超时是"防止损失扩大"。

### 共享账本：多 Agent 场景

当多个 Agent 并发运行时（spawn 子 Agent、ensemble 投票），它们需要共享一个预算上限。`BudgetLedger` 就是这个共享账本：

```
BudgetLedger
  ├── reserve(member, ...)   # 预留预算（防止超卖）
  ├── commit(member, ...)    # 结算实际花费
  ├── release(member, ...)   # 释放未使用的预留
  └── check(member)          # 检查是否超预算
```

> **为什么需要 reserve/commit？** 因为并发场景下，如果两个子 Agent 同时开始执行，它们都不知道对方会花多少。如果不预留，可能两个都执行完了才发现总预算超了——钱已经花了。reserve 就是"先占座"。

### cache_read 不计入 token 预算

```python
billable_tokens = total_tokens - run.cache_read_tokens
```

为什么？因为 Anthropic cache_read 只收 10% 费用，OpenAI 收 50%。如果全额计入预算，会出现"用了缓存反而更快耗尽预算"的反直觉行为。用户开缓存是为了省钱，不应该因为省钱而被预算限制。

但 cache_write 计入——因为 cache_write 是正常计费甚至有溢价。

### 反例

LangChain 的 `AgentExecutor` 只有 `max_iterations`。没有时间限制（模型卡住了只能等），没有 token 限制（上下文爆炸了才报错），没有成本限制（烧钱了才发现）。

AutoGen 的 `GroupChat` 有 `max_round`，但同样没有时间/token/cost 限制。

> **在你自己的项目里：** 不要只设一个"最大次数"。问自己——"这个操作可能以哪些方式失控？"次数、时间、资源量、成本——每种失控方式都需要一个独立的轴来限制。而且要实时检查，不要事后算账。

---

## 原则五：结构化错误而非异常——模型会犯错，这是正常现象

> 模型传错参数、调用不存在的工具，这不是异常，是正常反馈。返回结构化错误 + 修正建议，让模型自己修正。

### 为什么

传统编程中，"调用了不存在的函数"、"参数类型错误"是异常——因为这是程序员的 bug，应该崩溃让程序员修。

但在 Agent 系统中，"模型调用了不存在的工具"、"模型传错了参数"不是 bug，是**正常现象**。模型不是程序员，它会犯错，而且它能从错误中学习。

如果你用异常处理：

```python
# 错误做法：抛异常，打断整个循环
if tool_name not in tools:
    raise ValueError(f"Unknown tool: {tool_name}")
# 用户看到的是"程序崩了"，而不是"Agent 在学习修正"
```

如果你用结构化错误：

```python
# prodagent 的做法：返回结构化错误，模型下一轮自己修正
return ToolResult.from_error(
    ToolError.from_reason(
        ErrorReason.TOOL_NOT_AVAILABLE,
        message=f"Unknown tool: {tool_name}",
        hint=f"Available tools: {list(tools.keys())}"
    )
)
# 模型看到错误 + 修正建议，下一轮自己换一个工具
```

### ToolError 的设计

```python
@dataclass
class ToolError:
    reason: ErrorReason      # 受控词汇：驱动重试/严重程度决策
    code: str                # 自由格式：用于日志/消息
    error_severity: ErrorSeverity  # RED（不可重试）/ YELLOW（可重试）
    message: str = ""        # 给模型看的错误描述
    hint: str = ""           # 给模型看的修正建议
```

关键设计：

1. **reason 是受控词汇**——不是自由字符串。框架根据 reason 决定重试策略（YELLOW 重试、RED 终止）。
2. **severity 决定重试**——YELLOW（瞬时错误，如网络超时）可以重试；RED（永久错误，如参数错误）不重试。
3. **hint 是给模型的修正建议**——不是给程序员的 debug 信息。模型看到 hint 后能自己修正。

### ToolOutcome：6 种结果

```python
class ToolOutcome(StrEnum):
    OK = "ok"              # 成功
    RETRY = "retry"        # 可重试错误
    ABORT = "abort"        # 不可重试错误
    BLOCKED = "blocked"    # 被权限/策略拦截
    SUSPENDED = "suspended" # 挂起（等 HITL 审批）
    HANDOFF = "handoff"    # 接力（控制权转移给 peer）
```

每种结果对应不同的循环行为：

- OK → 继续下一轮
- RETRY → 模型看到错误，自己重试
- ABORT → 循环终止，标记 FAILED
- BLOCKED → 模型看到拦截原因，自己调整
- SUSPENDED → 循环挂起，等审批后恢复
- HANDOFF → 循环完成，控制权转移

### 反例

LangChain 的工具执行：模型传错参数 → 抛 `ValueError` → Agent 崩溃。用户看到的是 traceback，不是"Agent 在修正"。

AutoGen 的工具执行：类似，异常会传播到顶层。

> **在你自己的项目里：** 区分"程序员的错误"和"模型的错误"。程序员的错误用异常（应该崩溃修代码），模型的错误用结构化反馈（应该让模型自己修正）。给模型的错误信息要包含"修正建议"，而不是"debug 信息"。

---

## 原则六：五级压缩有明确边界——按 token 占比分级牺牲

> 不是"超过限制就截断"，而是"按占比分级牺牲，关键内容永远不被压缩"。

### 为什么

LLM 的上下文窗口是有限的。当对话越来越长，你必须做些什么。常见做法：

- **做法 A：截断**——只保留最近 N 条消息。简单，但丢失了早期的重要信息（比如用户的核心需求）。
- **做法 B：摘要**——把所有历史压缩成一段摘要。信息密度高，但摘要可能丢失细节，而且摘要本身可能出错。
- **做法 C：不处理**——让上下文自然增长，直到模型报错。简单，但不可靠。

prodagent 用五级压缩：按 token 占比分级牺牲，每一级有明确的触发条件和牺牲范围。

### 五级压缩全景

```
token 占比
  0% ──────────────────────────────────────────────── 100%
      │
      │  L0 系统提示（永不压缩）
      │  L1 状态块（永不压缩）
      │
      ├─ 0-25%   第 0 级：无压缩
      │             所有消息原样保留
      │
      ├─ 25-70%  第 1 级：工具结果压缩
      │             大的工具结果被截断/摘要
      │             对话历史不受影响
      │
      ├─ 70-85%  第 2 级：历史摘要
      │             早期对话被摘要成一段
      │             最近 N 条保留原样
      │
      ├─ 85-92%  第 3 级：主题摘要
      │             更多历史被摘要
      │             只保留最近 M 条
      │
      └─ 92-100% 第 4 级：紧急截断
                    只保留最后 2 条消息
                    防止上下文溢出
```

### L0-L3 分层预算

上下文窗口被分成 4 层，每层有固定的 token 预算比例：

```python
@dataclass
class ContextConfig:
    max_tokens: int = 100_000
    l0_ratio: float = 0.08   # 系统提示：8%
    l1_ratio: float = 0.15   # 状态块：15%
    l2_ratio: float = 0.35   # 记忆/注入：35%
    l3_ratio: float = 0.42   # 对话历史：42%
```

**L0（系统提示）永不压缩**——因为系统提示定义了 Agent 的身份和行为，压缩它会改变 Agent 的行为。

**L1（状态块）永不压缩**——状态块（turn 数、失败次数、最后动作）是 Agent 的"短期记忆"，压缩它会让 Agent 不知道自己在做什么。

**L2（记忆/注入）有独立预算**——记忆召回和技能注入有自己的 token 预算，不会被对话历史挤掉。

**L3（对话历史）是唯一被压缩的层**——五级压缩都作用在 L3 上。

### 压缩管道的设计

```python
HistoryCompressor([
    NoCompressionStage(),      # 第 0 级：无压缩
    ToolCompressStage(),       # 第 1 级：工具结果压缩
    SummarizeStage(            # 第 2 级：历史摘要
        recent_msgs=6,
        level=CompressionLevel.HISTORY_SUMMARY,
    ),
    SummarizeStage(            # 第 3 级：主题摘要
        recent_msgs=4,
        level=CompressionLevel.TOPIC_SUMMARY,
    ),
    EmergencyStage(),          # 第 4 级：紧急截断
])
```

每个 Stage 是一个独立的压缩策略，按顺序应用。当前一级的压缩不够时，自动降级到下一级。

### 反例

LangChain 的 `ConversationSummaryMemory`：要么全摘要，要么不摘要。没有分级，没有"关键内容不被压缩"的保护。

AutoGen 的对话管理：只保留最近 N 条，没有摘要，没有分级。

> **在你自己的项目里：** 不要"超过限制就截断"。先分层——哪些内容永不压缩（系统提示、核心约束），哪些内容可以压缩（历史对话），哪些内容可以丢弃（工具的详细输出）。然后分级——按资源占用比例，从轻到重逐级牺牲。每一级有明确的触发条件和牺牲范围。

---

## 原则七：单 Agent 是默认——先做好上下文管理，再拆多 Agent

> 很多"需要多 Agent"的场景，其实是单 Agent 的上下文管理没做好。

### 为什么

多 Agent 听起来很酷——"专家团队协作"、"分工合作"、"并行加速"。但多 Agent 的代价被严重低估了：

| 代价 | 说明 |
|------|------|
| 更贵 | 每个 Agent 各消耗 token，总开销可能是单 Agent 的 2-5 倍 |
| 更慢 | 通信 overhead、协调开销、等待其他 Agent 的开销 |
| 更难调试 | 问题出在哪个 Agent？是 A 的输出错了还是 B 的理解错了？ |
| 可能死循环 | A 推 B，B 推 A，无限循环 |
| 上下文丢失 | Agent 之间传递信息时，细节可能丢失 |

prodagent 的哲学：**先把单 Agent 的上下文管理做到极致，实在搞不定再拆多 Agent。**

### 单 Agent 的上下文管理工具箱

prodagent 为单 Agent 提供了丰富的上下文管理工具：

1. **四通道记忆**——规则/实体/精确/语义并行召回，让 Agent 记住重要信息
2. **五级压缩**——按 token 占比分级牺牲，关键信息不丢失
3. **技能系统**——成功 run 蒸馏成 runbook，越用越稳
4. **工具结果 spill**——大的工具结果溢出到外部存储，只保留预览
5. **系统提示分层**——L0 系统提示 + L1 状态块 + L2 记忆注入 + L3 对话历史

很多时候，把这些工具用好，单 Agent 就能搞定"看起来需要多 Agent"的场景。

### 什么时候该拆多 Agent

当然，有些场景确实需要多 Agent：

| 场景 | 为什么单 Agent 搞不定 | 推荐拓扑 |
|------|---------------------|---------|
| 需要真正的并行 | 多个独立任务同时执行，单 Agent 只能串行 | spawn（委派） |
| 需要不同的专业身份 | 不同角色需要不同的系统提示和工具集 | ensemble（投票） |
| 需要接力式处理 | A 做完交给 B，B 做完交给 C | peer（接力） |
| 需要共享状态协作 | 多个专家读写同一份共享数据 | blackboard（黑板） |
| 需要任务队列 | 生产者-消费者模式，动态分配任务 | work_queue（队列） |

### 五协作原语的选择指南

```
任务能并行吗？
  ├─ 是 → spawn（委派子 Agent 并行执行）
  └─ 否 → 需要接力吗？
            ├─ 是 → peer（接力，控制权转移）
            └─ 否 → 需要共享状态吗？
                      ├─ 是 → blackboard（黑板，专家读写共享数据）
                      └─ 否 → 需要投票/辩论吗？
                                ├─ 是 → ensemble（投票，多 Agent 讨论）
                                └─ 否 → 需要任务队列吗？
                                          ├─ 是 → work_queue（队列）
                                          └─ 否 → 用单 Agent！
```

### 反例

AutoGen 默认就是多 Agent——`AssistantAgent` + `UserProxyAgent` 是标配。很多简单的问答场景也被迫用多 Agent，开销大、调试难。

LangGraph 鼓励用图来编排多 Agent，但很多用户用图来编排单 Agent 的步骤——过度设计。

> **在你自己的项目里：** 不要一上来就设计多 Agent 系统。先问——"单 Agent + 好的上下文管理能搞定吗？"如果能，就用单 Agent。如果不能，再问——"我需要哪种协作拓扑？"不要用"多 Agent"作为默认答案，用它作为最后手段。

---

## 原则八：所有拓扑共用一个消息平面——在一个地方解决"不丢不重不乱序"

> 五种协作原语的通信都走 Crossing 管道。去重、契约、安全、审计、死信，在一个地方解决。

### 为什么

多 Agent 系统最容易出问题的地方不是"Agent 不够聪明"，而是"消息不可靠"：

- **消息丢失**——A 发给 B 的消息因为网络问题丢了，B 永远等不到
- **消息重复**——A 重试时发了两次，B 执行了两次（副作用翻倍）
- **消息乱序**——A 先发的消息后到，B 的状态错乱
- **消息越权**——A 发给 B 的消息包含了不应该让 B 看到的信息
- **消息格式错误**——A 发的消息不符合 B 的预期，B 解析失败

如果每种协作拓扑（spawn/peer/ensemble/board/queue）各自实现消息传递，这些问题要在 5 个地方各解决一遍。而且 5 个地方的实现可能不一致——spawn 的去重和 peer 的去重逻辑不一样，调试噩梦。

prodagent 的做法：**所有拓扑共用一个消息平面**。

### Crossing 消息平面

```
Crossing（跨 Agent 边界的消息信封）
  │
  ├── DOWNSTREAM（下游：父→子、调度→执行）
  │     └── assembly_pipeline（组装管道）
  │           ├── 去重（idempotency）
  │           ├── 契约校验（contract）
  │           ├── 安全拦截（gate）
  │           ├── 审计记录（audit）
  │           └── 死信队列（dead_letter）
  │
  └── UPSTREAM（上游：子→父、执行→调度）
        └── admission_pipeline（准入管道）
              ├── 去重（idempotency）
              ├── 契约校验（contract）
              ├── 输出截断（trim）
              ├── 安全拦截（gate）
              ├── 审计记录（audit）
              └── 死信队列（dead_letter）
```

### 五道关卡详解

**1. 去重（Idempotency）**

每条消息有唯一 ID。接收方维护"已处理消息 ID"集合，重复消息直接丢弃。

```python
# coordination/messaging/idempotency.py
class IdempotencyInterceptor:
    async def process(self, crossing):
        if await self._seen.contains(crossing.id):
            return None  # 重复，丢弃
        await self._seen.add(crossing.id)
        return crossing
```

**2. 契约校验（Contract）**

每个 Agent 可以声明它的输出契约（比如"输出必须是 JSON，包含 result 字段"）。消息平面校验输出是否符合契约，不符合的进入死信队列。

```python
# coordination/messaging/contract.py
class MessageContract:
    async def validate(self, payload):
        # 校验 JSON 格式、必填字段、类型
        ...
```

**3. 安全拦截（Gate）**

消息经过 `HookRegistry.check_blocking()`，权限策略、提示注入检测、内容过滤都在这里。

**4. 审计记录（Audit）**

每条跨边界消息都记录审计日志：谁发给谁、什么时间、消息摘要。

**5. 死信队列（Dead Letter）**

任何一关失败的消息进入死信队列，不丢失、可重试、可人工介入。

### 为什么这很重要

因为"不丢不重不乱序"是分布式系统的经典难题。在一个地方解决它，比在 5 个地方各解决一遍更可靠、更易维护、更易调试。

而且，当你需要加一个新的横切关注点（比如"消息加密"），只需要在消息平面加一个 interceptor，5 种拓扑同时获得这个能力。

### 反例

AutoGen 的 `GroupChat`：消息传递是直接的函数调用，没有去重、没有契约、没有死信。Agent 崩溃了消息就丢了，重试了消息就重复了。

LangGraph 的多 Agent：消息通过状态图传递，有一定的可靠性保证，但每种图结构的消息处理逻辑不一样，没有统一平面。

> **在你自己的项目里：** 如果你有多个组件/服务之间通信，不要让每个组件自己实现消息可靠性。建立一个统一的消息平面/总线，在一个地方解决去重、契约、安全、审计、死信。新的横切关注点只需要加在平面上，所有组件同时受益。

---

## 原则九：乐观并发恢复——不需要分布式锁，因为冲突概率极低

> checkpoint + 版本控制，kill -9 后续跑不重复执行。

### 为什么

Agent 运行到一半崩溃了（`kill -9`、机器断电、OOM），怎么恢复？

传统做法：**分布式锁 + 事务**。运行前加锁，运行中每个状态变更都写事务，恢复时从事务日志重放。

问题：
- 分布式锁难实现、易死锁、需要维护锁服务
- 事务日志重放慢、复杂
- 为了"极低概率的冲突"付出了"极高的实现复杂度"

prodagent 的做法：**乐观并发**。

### 乐观并发的核心思想

```
1. 读取当前状态 + 版本号
2. 修改状态
3. 写入时检查版本号：
   - 版本号一致 → 写入成功，版本号 +1
   - 版本号不一致 → 冲突，重新加载最新状态，重试
```

因为 Agent Run 的写入冲突概率极低（一个 Run 通常只有一个执行者），乐观并发的冲突重试几乎不会发生。但一旦发生，它能正确处理。

### checkpoint 的设计

```python
# ports/checkpoint.py
class CheckpointStore(Protocol):
    async def save(self, run: AgentRun, *, expected_version: int) -> None: ...
    async def load(self, run_id: str) -> AgentRun | None: ...
```

每次保存 checkpoint 时传入 `expected_version`。如果存储中的版本号和 `expected_version` 不一致，说明有并发写入，抛出 `VersionConflict`。

### 恢复流程

```
崩溃恢复：
  1. 从 checkpoint 加载 AgentRun（包含 messages、metrics、pending_*）
  2. 状态是 RUNNING 或 SUSPENDED → 可以恢复
  3. 如果有 pending_tool_call（等审批的工具）→ 重试这个调用
  4. 如果有 pending_handoff（等接力的）→ 执行接力
  5. 否则 → 从下一个 Step 开始继续循环
```

### 为什么不会重复执行

因为 Step 是原子的——要么完整执行一次，要么不执行。checkpoint 在 Step 之间保存：

```
Step 1 完成 → checkpoint 保存（状态包含 Step 1 的结果）
Step 2 进行中 → 崩溃
恢复 → 从 checkpoint 加载（Step 1 已完成）→ 从 Step 2 开始
```

Step 2 没有被保存（因为还没完成），所以恢复时会重新执行 Step 2。但 Step 1 已经保存了，不会重复执行。

> **工具的幂等性**：如果 Step 2 执行了一半（工具调用了但结果没写回），恢复时会重新调用工具。这就是为什么 `enforced_idempotent=True` 的工具会被注入 `idempotency_key`——工具函数可以用这个 key 保证幂等。

### 反例

LangChain 的 `AgentExecutor`：没有 checkpoint，崩溃了就全丢了。想恢复要自己实现。

AutoGen：没有内置的崩溃恢复机制。

> **在你自己的项目里：** 不要一上来就用分布式锁。先问——"冲突概率有多高？"如果极低（比如一个用户一个会话），用乐观并发就够了。checkpoint + 版本号，简单、可靠、不需要额外基础设施。只有当真的有高并发写入时，才需要分布式锁。

---

## 原则十：全离线可复现——理解一个机制最深刻的方式是调试它

> 1,182 个测试零 API key、零网络、零 flaky。FakeLLM 精确控制每一轮输出。

### 为什么

Agent 框架的测试有一个经典难题：**依赖 LLM API**。

如果测试要调用真实的 LLM API：
- **慢**——每个测试要等网络往返，全量测试可能要几十分钟
- **贵**——每次跑测试都要花钱
- **flaky**——模型输出不确定，同一个输入可能得到不同输出，测试时过时不过
- **需要 API key**——新贡献者要先配置 API key 才能跑测试
- **无法离线开发**——在飞机上、在没有网络的环境里无法工作

prodagent 的做法：**FakeLLM**。

### FakeLLM 的设计

```python
# llm/fake.py
class FakeLLMAdapter:
    def __init__(self, script: list[LLMResponse]):
        self._script = script  # 预设的每一轮输出
        self._index = 0

    async def complete(self, messages, *, system, tools, config, on_chunk):
        response = self._script[self._index]
        self._index += 1
        return response
```

FakeLLM 按脚本逐轮返回预设的 `LLMResponse`。测试可以精确控制每一轮模型的输出：

```python
def test_tool_call_loop():
    llm = FakeLLMAdapter(script=[
        # 第 1 轮：模型决定调用搜索工具
        LLMResponse(
            content="",
            tool_calls=[ToolCall(name="search", params={"query": "weather"})],
            stop_reason=StopReason.TOOL_USE,
        ),
        # 第 2 轮：模型看到工具结果，给出最终答案
        LLMResponse(
            content="巴黎今天晴天，25度。",
            stop_reason=StopReason.END_TURN,
        ),
    ])
    # ... 运行循环，断言行为
```

### 这带来了什么

**1. 测试确定性**——同一个测试跑 100 次，结果完全一样。没有 flaky。

**2. 测试速度**——1,182 个测试在 30 秒内跑完。因为没有网络往返。

**3. 零成本**——跑测试不花一分钱。

**4. 可离线**——不需要 API key，不需要网络。新贡献者 clone 下来就能跑测试。

**5. 可调试**——测试失败时，你可以精确知道"模型在第几轮返回了什么"，因为输出是你预设的。

### script 辅助函数

为了方便写测试，prodagent 提供了 `script()` 辅助函数：

```python
from prodagent.llm.fake import script

# 用简洁的 DSL 写脚本
llm = script("""
[
  {"tool_calls": [{"name": "search", "params": {"query": "weather"}}]},
  {"content": "巴黎今天晴天。"}
]
""")
```

### 反例

LangChain 的测试：很多测试依赖真实 API，需要 `OPENAI_API_KEY` 环境变量。CI 里要配置 secret，本地开发要自己有 key。测试慢、贵、flaky。

AutoGen 的测试：类似，很多测试需要真实 API。

> **在你自己的项目里：** 如果你的系统依赖外部服务（LLM API、数据库、第三方 API），为测试写一个 Fake 实现。不要在测试里调用真实服务。Fake 实现应该能精确控制每一步的输出，让测试确定性、快速、低成本、可离线。理解一个机制最深刻的方式是调试它——而调试需要确定性。

---

## 总结：10 条原则的内在联系

这 10 条原则不是孤立的，它们有内在的逻辑链条：

```
原则零（先写注释）
  → 原则一（裸核默认）：因为你能说清"为什么默认这样"
    → 原则二（纯内核）：因为你能说清"为什么 kernel 不依赖外部"
      → 原则三（三协议总线）：因为 kernel 纯了，横切关注点需要一个统一接缝
        → 原则四（四轴预算）：因为循环是纯的，预算检查可以在关键节点实时进行
          → 原则五（结构化错误）：因为循环不抛异常，错误是反馈而非崩溃
            → 原则六（五级压缩）：因为上下文管理是独立模块，有明确的分层预算
              → 原则七（单 Agent 默认）：因为上下文管理做好了，多 Agent 不是必需
                → 原则八（统一消息平面）：因为多 Agent 是可选的，消息平面要统一
                  → 原则九（乐观并发恢复）：因为消息可靠了，恢复可以用简单的乐观并发
                    → 原则十（全离线可复现）：因为所有机制都可测试，FakeLLM 让测试确定
```

每一条原则都为下一条创造了条件。这就是架构的"美感"——不是单个设计有多巧妙，而是所有设计环环相扣、互相支撑。

---

## 下一步

- 想看这些原则在代码里的具体体现？→ [架构全景](architecture.md)
- 想跟着一次调用走完整个生命周期？→ [心智模型](mental-model.md)
- 想看每个关键决策的"为什么不选另一种"？→ [设计取舍](decisions.md)
- 想把这些原则用到自己的项目？→ [5 分钟上手](start.md)
