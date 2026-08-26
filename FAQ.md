# 常见问题（FAQ）

> 关于 prodagent 最常被问到的问题。如果你的问题不在列表里，欢迎提 Issue 或 Discussion。

---

## 目录

- [基础概念](#基础概念)
- [安装与配置](#安装与配置)
- [架构与设计](#架构与设计)
- [使用问题](#使用问题)
- [多 Agent](#多-agent)
- [生产部署](#生产部署)
- [对比其他框架](#对比其他框架)
- [贡献与社区](#贡献与社区)

---

## 基础概念

### Q: prodagent 是什么？

A: prodagent 是一个生产级 LLM Agent 框架。它提供了构建可靠、可观测、安全的 Agent 系统所需的所有核心机制——循环、预算、恢复、审批、权限、可观测、多 Agent 协作。它的代码小到你能从头读到尾（25,000 行），同时完整到能直接上生产。

### Q: prodagent 和"又一个 Agent 框架"有什么不同？

A: 三个核心差异：

1. **可读**——25,000 行代码，按学习顺序排列，注释解释"为什么"而不是"做什么"。你能在脑子里建立完整的心智模型。
2. **生产级**——内建预算、恢复、审批、权限、可观测，不是"加几个插件就叫生产级"。
3. **可拆解**——每个机制都是独立模块，有清晰的 Protocol 边界。你的项目缺哪块就搬哪块，不用引入整个框架。

### Q: 我需要懂 Agent 相关的论文才能用吗？

A: 不需要。prodagent 把论文里的概念（ReAct、Plan-and-Solve、Multi-Agent 等）实现成了可用的机制，你直接用就行。但如果你想理解"为什么这么设计"，文档里有详细的解释。

### Q: prodagent 支持哪些模型？

A: 任何有 OpenAI 兼容 API 的模型都支持，包括：

- OpenAI（GPT-4o, GPT-4o-mini, o1 等）
- Anthropic（Claude 3.5 Sonnet, Claude 3 Opus 等）——原生适配
- DeepSeek（V2, V3, R1）
- Qwen（通义千问）
- Moonshot（Kimi）
- 本地模型（Ollama, llama.cpp 等）——通过 OpenAI 兼容端点

只需要设置 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 环境变量。

---

## 安装与配置

### Q: 安装 prodagent 需要什么？

A: Python 3.11+ 和 pip。核心安装只有 4 个依赖：

```bash
pip install prodagent
```

按需加装模型 provider 和生产后端：

```bash
pip install "prodagent[openai,anthropic,postgres,redis]"
```

### Q: 没有 API key 能试用吗？

A: 能！设置 `USE_FAKE_LLM=1` 环境变量，prodagent 会用内置的 FakeLLM 完全离线运行。这是学习和测试的推荐方式——快速、确定、零成本。

### Q: 怎么配置模型？

A: 三种方式（任选其一）：

```bash
# 方式 1：完全离线（学习/测试用）
export USE_FAKE_LLM=1

# 方式 2：OpenAI 兼容端点（DeepSeek/Qwen/Moonshot/本地模型）
export LLM_BASE_URL=https://api.deepseek.com/v1
export LLM_API_KEY=your-key
export LLM_MODEL=deepseek-chat

# 方式 3：Anthropic 原生
export ANTHROPIC_API_KEY=your-key
```

### Q: 怎么开启生产模式？

A: 一行代码：

```python
from prodagent import Agent, AgentConfig
from prodagent.base.config import production

agent = Agent("demo", tools=[...],
              config=AgentConfig(name="demo", framework=production()))
```

`production()` 会开启：落盘恢复、span 追踪、HIGH 工具审批、LLM 缓存、上下文压缩、工具结果 spill。

---

## 架构与设计

### Q: 为什么用 Protocol 而不是 ABC（抽象基类）？

A: 因为 Protocol 是结构类型（鸭子类型），不需要继承。用户用自己的 OpenAI 客户端，只要有 `async def complete(...)` 方法，就能直接用——不需要继承 prodagent 的基类。这是"框架不绑架用户"的基础。

详见 [设计哲学 · 原则三](docs/design-philosophy.md#原则二三协议总线一个接缝连接所有横切关注点)。

### Q: 为什么核心只有 4 个依赖？

A: 刻意的设计选择。核心依赖越少：

1. 安装越快、冲突越少
2. 安全漏洞面越小
3. 用户越容易把框架嵌入自己的项目
4. 框架越容易维护（不需要追着上游 breaking change 跑）

所有其他依赖（openai SDK、redis、postgres 驱动等）都是 optional，用户按需安装。

### Q: 为什么把 kernel 从 runtime 里拆出来？

A: 因为"循环逻辑"和"Agent 装配逻辑"是不同的关注点。拆分后：

- kernel 可以独立测试（不需要构造完整 Agent）
- kernel 可以独立替换（你可以写自己的循环实现）
- 边界清晰（kernel 不知道 LLM 是 OpenAI 还是 Anthropic）

类比：把"发动机"从"整车"里拆出来——发动机可以独立测试、独立替换。

### Q: 三协议总线（fire/check/collect）是什么？

A: 这是 prodagent 最精妙的设计之一。横切关注点（审批、可观测、记忆、审计）通过三种语义不同的协议接入：

- **fire（观察）**——"发生了一件事，你们随便看"。并发扇出，失败只记录。
- **check（拦截）**——"这件事能不能做？你们说了算"。串行执行，第一个否决就停止，fail-closed。
- **collect（注入）**——"你们有什么要加进来的？都给我"。并发收集，失败降级为 None。

循环本身不知道审批、可观测、记忆的名字——它只说"我要调用工具了"，然后总线决定谁来监听、谁来拦截、谁来注入。

详见 [设计哲学 · 原则三](docs/design-philosophy.md#原则三三协议总线一个接缝连接所有横切关注点)。

### Q: 为什么是四轴预算而不是一轴？

A: 因为任何单一轴都可能被绕过：

- 只有 turns → 20 轮可能花 $50（每轮 100k token）
- 只有 cost → 死循环时每轮花很少钱，但永远不停
- 只有 tokens → 不同模型价格差异巨大
- 只有 seconds → API 很快返回但烧了很多 token

四轴互为兜底——任一轴触顶即停，同时生效就很难失控。

详见 [设计哲学 · 原则四](docs/design-philosophy.md#原则四四轴预算互为兜底)。

---

## 使用问题

### Q: 怎么定义一个工具？

A: 用 `@tool` 装饰器：

```python
from prodagent import tool, SideEffectLevel

@tool(name="search", readonly=True)
async def search(query: str) -> str:
    """搜索网络信息。"""
    return f"results for: {query}"

@tool(name="send_email", side_effect_level=SideEffectLevel.HIGH)
async def send_email(to: str, subject: str, body: str) -> str:
    """发送邮件。HIGH 副作用工具会触发 HITL 审批。"""
    # 发送逻辑
    return "Email sent"
```

### Q: 只读工具和写工具有什么区别？

A: 执行方式不同：

- **只读工具**（`readonly=True`）——可以并行执行（默认最多 8 个并发）
- **写工具**（`readonly=False`）——必须串行执行（防止竞态条件）

这是安全优先的默认策略。如果你的写工具之间没有依赖，可以自定义 dispatcher 实现并行。

### Q: HIGH 副作用工具的审批是怎么工作的？

A: 当模型调用 `side_effect_level=HIGH` 的工具时：

1. 运行挂起（`RunState.SUSPENDED`）
2. 生成审批请求，包含工具名、参数、上下文
3. 等待人工审批（通过 `agent.submit_approval(request_id, decision)`）
4. 审批通过 → 继续执行工具
5. 审批拒绝 → 模型看到拒绝原因，自己调整（可能换一个工具或修改参数）

这是"人在回路"（Human-in-the-Loop）的标准实现。

### Q: 怎么实现崩溃恢复？

A: 开启生产模式后自动生效：

```python
from prodagent.base.config import production

agent = Agent("demo", tools=[...],
              config=AgentConfig(name="demo", framework=production()))

# 第一次运行（可能中途崩溃）
result = await agent.chat("复杂任务", session_id="session-123")

# 崩溃后恢复（用同一个 session_id）
result = await agent.chat(resume=True, session_id="session-123")
```

prodagent 会从 checkpoint 恢复状态，从崩溃的地方继续执行，不会重复执行已完成的步骤。

### Q: 怎么用 PLAN_FIRST 模式？

A: 设置 `mode=ExecutionMode.PLAN_FIRST`：

```python
from prodagent import Agent, ExecutionMode

agent = Agent("planner", tools=[...], mode=ExecutionMode.PLAN_FIRST)
result = await agent.chat("复杂的多步骤任务")
```

PLAN_FIRST 模式下：

1. 先调用一次 LLM 生成计划 DAG（步骤 + 依赖关系）
2. 按 DAG 拓扑顺序执行步骤（可并行的步骤并行执行）
3. 步骤失败 → 增量重规划（只替换失败的步骤及其后续）
4. 所有步骤完成 → 汇总结果

### Q: 怎么用 Workflow？

A: Workflow 是预定义的静态 DAG，不由 LLM 生成：

```python
from prodagent.plan.workflow import Workflow, step

@step
def search(query: str) -> str:
    return f"results for: {query}"

@step(depends_on=["search"])
def summarize(results: str) -> str:
    return f"summary of: {results}"

workflow = Workflow(steps=[search, summarize])
agent = Agent("workflow", workflow=workflow)
```

Workflow 适合流程固定的业务场景（审批流、数据处理管道），确定性高。

---

## 多 Agent

### Q: 什么时候该用多 Agent？

A: prodagent 的哲学是**先把单 Agent 的上下文管理做好，实在搞不定再拆多 Agent**。很多"需要多 Agent"的场景，其实是单 Agent 的上下文管理没做好。

确实需要多 Agent 的场景：

- 需要真正的并行（多个独立任务同时执行）
- 需要不同的专业身份（不同角色需要不同的系统提示和工具集）
- 需要接力式处理（A 做完交给 B，B 做完交给 C）
- 需要共享状态协作（多个专家读写同一份共享数据）
- 需要任务队列（生产者-消费者模式）

详见 [设计哲学 · 原则七](docs/design-philosophy.md#原则七单-agent-是默认先做好上下文管理再拆多-agent)。

### Q: 五种协作原语怎么选？

A: 决策树：

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

### Q: spawn 和 peer 有什么区别？

A: 核心区别是**控制权是否返回**：

- **spawn（委派）**——父 Agent 调用子 Agent，等待子 Agent 完成，结果返回给父 Agent，父 Agent 继续运行。类似函数调用。
- **peer（接力）**——父 Agent 把控制权转移给 peer，父 Agent 的运行结束（COMPLETED），peer 继续运行。类似接力赛跑。

### Q: 多 Agent 之间怎么保证消息不丢不重不乱序？

A: 通过统一消息平面（Crossing）。所有跨 Agent 边界的消息都经过五道关卡：

1. **去重**——每条消息有唯一 ID，重复消息直接丢弃
2. **契约校验**——校验消息格式是否符合接收方的预期
3. **安全拦截**——权限检查、提示注入检测、内容过滤
4. **审计记录**——记录谁发给谁、什么时间、消息摘要
5. **死信队列**——任何一关失败的消息进入死信队列，不丢失

详见 [设计哲学 · 原则八](docs/design-philosophy.md#原则八所有拓扑共用一个消息平面在一个地方解决不丢不重不乱序)。

---

## 生产部署

### Q: prodagent 能上生产吗？

A: 能。`production()` 一键开启全套生产护甲：

- 落盘恢复（checkpoint + 事件日志）
- span 追踪（OpenTelemetry 兼容）
- HIGH 工具 HITL 审批
- LLM 响应缓存
- 上下文压缩 + 工具结果 spill
- 四轴硬预算
- 死循环检测
- 工具熔断器

后端支持：

- **单机**：file（持久化状态）+ memory（临时状态）
- **多副本**：postgres（持久化状态）+ redis（临时状态）+ neo4j（图数据）

### Q: 怎么选择后端？

A: 按数据特性选引擎，不是"一个数据库存所有东西"：

| 数据类型 | 单机 | 多副本 | 说明 |
|---------|------|--------|------|
| checkpoint / session / event_log / span | file | postgres | 关系型/持久化状态 |
| cache / lock / dead_letter | memory | redis | 临时/在途状态 |
| graph（事实图谱） | file | neo4j | 图数据，图查询在关系型数据库里是灾难 |

通过环境变量配置：

```bash
# 全部用生产后端
export PRODAGENT_BACKEND=prod

# 或者混合配置
export PRODAGENT_BACKEND_CHECKPOINT=postgres
export PRODAGENT_BACKEND_CACHE=redis
export PRODAGENT_BACKEND_GRAPH=neo4j
```

### Q: 怎么监控生产环境的 Agent？

A: prodagent 提供了多层可观测性：

1. **事件总线**——所有关键事件（TURN_START, LLM_REQUEST, TOOL_CALL, TOKEN_UPDATE 等）都通过 `fire` 发出，你可以挂载自己的观察者
2. **Span 导出**——`SpanExporter` 端口，当前有 file 和 postgres 实现，可以对接 OpenTelemetry
3. **控制台观察者**——`ConsoleObserver` 把事件打印到控制台（开发调试用）
4. **缓存监控**——`CacheMonitor` 监控 LLM 缓存命中率

生产环境建议：挂载一个自定义观察者，把事件发送到你的监控系统（Prometheus / Datadog / 自建）。

### Q: 怎么控制成本？

A: 多层成本控制：

1. **四轴预算**——`max_cost_usd` 直接限制单次运行的最大成本
2. **LLM 缓存**——生产模式默认开启，重复的上下文调用命中缓存，成本降低 50-90%
3. **模型选择**——用便宜的模型做规划/总结，用贵的模型做关键决策
4. **上下文压缩**——减少每次调用的 token 数
5. **工具结果 spill**——大的工具结果溢出到外部存储，不占用上下文窗口

---

## 对比其他框架

### Q: prodagent vs LangChain / LangGraph？

A:

| 维度 | LangChain / LangGraph | prodagent |
|------|----------------------|-----------|
| 定位 | 工具集 / 状态机编排 | 完整的 Agent 运行时 |
| 代码量 | 数十万行，抽象层厚 | 25,000 行，按学习顺序排列 |
| 生产机制 | 需要自己组装 / 绑死云 | 内建，默认开启 |
| 核心依赖 | 数十个（间接） | 4 个 |
| 测试 | 依赖真实 API | 1,182 个全离线 |
| 你学会的是 | 它的 API | Agent 系统的设计心智模型 |

LangChain 适合快速原型和工具集成。prodagent 适合需要理解底层机制、需要生产级可靠性、或者想学习 Agent 系统设计的场景。

### Q: prodagent vs AutoGen？

A:

| 维度 | AutoGen | prodagent |
|------|---------|-----------|
| 核心 | 多 Agent 对话 | 完整的 Agent 运行时（含多 Agent） |
| 单 Agent | 较弱（默认多 Agent） | 强（单 Agent 是默认） |
| 生产机制 | 较少 | 内建全套 |
| 消息可靠性 | 直接函数调用 | 统一消息平面（去重/契约/死信） |
| 测试 | 依赖真实 API | 全离线 |

AutoGen 适合多 Agent 对话场景。prodagent 适合从单 Agent 到多 Agent 的全场景，且有完整的生产机制。

### Q: prodagent vs CrewAI？

A: CrewAI 聚焦于"角色化的多 Agent 团队"，概念简单易用。prodagent 是更底层的运行时框架，提供了更细粒度的控制和更完整的生产机制。如果你需要快速搭建一个"团队"，CrewAI 可能更简单；如果你需要理解和控制底层机制，prodagent 更合适。

### Q: 我能把 prodagent 和其他框架一起用吗？

A: 能。因为 prodagent 的核心依赖很少，且通过 Protocol 抽象，你可以：

- 用 LangChain 的工具作为 prodagent 的工具（适配一下接口）
- 用 prodagent 的 Agent 作为 LangChain 的一个节点
- 用 prodagent 的记忆/压缩模块，用其他框架的循环
- 任何组合，只要接口适配

---

## 贡献与社区

### Q: 我想贡献代码，从哪里开始？

A: 见 [贡献指南](CONTRIBUTING.md)。建议：

1. 先读文档，理解架构
2. 跑通测试，确保环境正常
3. 找一个 `good first issue` 标签的任务
4. 或者从文档改进、测试改进开始

### Q: 我能只用到 prodagent 的一部分吗？

A: 能！这是 prodagent 的核心设计目标之一。每个机制都是独立模块：

- 只想要四轴预算 → 用 `kernel/budget.py`
- 只想要上下文压缩 → 用 `cognition/context/`
- 只想要工具调度 → 用 `tooling/dispatcher.py`
- 只想要死循环检测 → 用 `kernel/progress.py`
- 只想要记忆系统 → 用 `cognition/memory/`

因为核心依赖只有 4 个，引入单个模块的开销很小。

### Q: 怎么获取帮助？

A:

- **文档**——先查文档，大部分问题都有答案
- **GitHub Discussion**——提问、讨论、分享
- **GitHub Issue**——报告 bug、提议功能
- **极客时间专栏**——《生产级 Agent 排雷实战》是 prodagent 的配套专栏，有更详细的讲解

### Q: prodagent 的许可证是什么？

A: AGPL-3.0-only。这意味着：

- 你可以自由使用、修改、分发
- 如果你修改了 prodagent 并通过网络提供服务（SaaS），你需要公开你的修改
- 如果你只是在自己的产品里使用 prodagent（不修改源码），不需要公开你的产品代码
- 如果你需要商业许可证（不想要 AGPL 的约束），可以联系维护者

### Q: 怎么支持 prodagent？

A:

- ⭐ **点 Star**——让更多人看到这个项目
- 🐛 **提 Issue**——报告 bug、提议功能
- 🔧 **提 PR**——贡献代码、文档、测试
- 💬 **参与讨论**——在 Discussion 里分享你的想法和使用经验
- 📢 **分享**——在社交媒体、博客、演讲中介绍 prodagent
- 💰 **赞助**——如果 prodagent 对你有帮助，考虑赞助维护者

> **你的每一个 Star、每一个 Issue、每一个 PR，都让这个框架变得更好。**

---

## 还有问题？

如果这份 FAQ 没有回答你的问题，欢迎：

- 在 GitHub Discussion 提问
- 提一个 Issue（标签：question）
- 阅读 [完整文档](docs/index.md)
- 阅读 [设计哲学](docs/design-philosophy.md)（很多"为什么"的答案在这里）
