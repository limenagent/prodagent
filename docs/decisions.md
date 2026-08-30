# 设计取舍：为什么这么做，而不是那么做

> 每一个关键决策背后都有取舍。这一页记录"为什么不选另一种"，帮你理解框架的设计哲学，也帮你在自己的项目里做判断。

---

## 架构层

### 为什么用 Protocol 而不是 ABC？

| | ABC（抽象基类） | Protocol（结构类型） |
|---|---|---|
| 适配第三方类 | 需要改继承关系 | 零修改，只要方法签名匹配 |
| 运行时检查 | 检查继承链 | 检查结构（鸭子类型） |
| 多接口组合 | 单继承限制 | 一个类可同时满足多个 Protocol |
| 框架侵入性 | 高（必须继承我的基类） | 低（不需要导入任何东西） |

**选择 Protocol 的核心理由**：框架不应该要求用户继承自己的基类。用户用自己的 OpenAI 客户端，只要有 `async def complete(...)` 方法，就能直接用。这是"框架不绑架用户"的基础。

---

### 为什么核心只有 4 个依赖？

核心依赖：`anyio` / `httpx` / `pydantic` / `typing-extensions`。

**刻意不引入的**：
- 不引入 `openai` SDK——通过 `LLMClient` Protocol 适配，用户用什么模型都行
- 不引入 `redis` / `postgres`——通过端口抽象，默认用 file/memory
- 不引入 `langchain` / `llamaindex`——不依赖其他框架
- 不引入 `fastapi`——playground 是可选的 extra

**理由**：核心依赖越少，
1. 安装越快、冲突越少
2. 安全漏洞面越小
3. 用户越容易把框架嵌入自己的项目
4. 框架越容易维护（不需要追着上游 breaking change 跑）

> 对比：LangChain 的核心包有数十个间接依赖，安装一次可能要下载几百 MB。

---

### 为什么把 kernel 从 runtime 里拆出来？

最近的重构把 `kernel/` 从 `runtime/` 里独立出来。

**拆分前的问题**：
- 循环逻辑和 Agent 装配逻辑混在一起
- PLAN_FIRST 和 REACTIVE 共享代码但边界不清
- 测试需要 mock 很多 runtime 层的东西

**拆分后**：
- `kernel/` — 纯循环逻辑（Step、Loop、Budget、Bus），不依赖 runtime
- `runtime/` — Agent 装配、工厂、父运行时，依赖 kernel
- 两种执行模式（REACTIVE / PLAN_FIRST）共享同一个 Step
- kernel 可以独立测试，不需要构造完整 Agent

**类比**：就像把"发动机"从"整车"里拆出来——发动机可以独立测试、独立替换，整车负责装配。

---

### 为什么执行一个 agent 也走端口？

spawn 里直接 `import` runner 调用、舞台成员直接 `agent.chat()`，单进程下没有问题——但协作代码从此和进程内执行绑死：把任何一个成员挪到别的机器上，要改的就是协作层。

prodagent 的做法：激活一次执行就是一次端口调用（`ports/execution.py`）。`RunnerPort.activate()` 的入参 `AgentActivation` 只带可序列化字段——agent、任务、run_id、账本，或 session_id；进程内实现 `InProcessRunner` 持有本跳的 hooks/checkpoint/账本并负责子 agent 的 fork，成员会话用更轻的 `InProcessChatRunner`。接力同理：relay 返回 `HandoffActivation`，peer 查找与 fork 由驱动方解释，协作层不构造运行时对象。

**理由**：这让"`coordination` 不 import `runtime`"成为一条 CI 能检查的红线（两个方向都有测试）。换分布式执行就是换一个端口实现，协作原语一行不改。

---

### 为什么 AgentSpec 和 AgentConfig 是两个东西？

`AgentConfig` 里是 LLM 客户端、hooks 注册表、存储、工具实例——只在当前进程有意义，序列化不了。远程派活需要传递的是另一组信息：名字、系统提示、模式、预算、工具 schema、子 agent 与同伴的规格。

所以投影是显式的一步：`Agent.spec()` 产出纯数据的 `AgentSpec`（`ports/execution.py`），`to_dict` / `from_dict` 无损往返。spawn 工具给模型看的子 agent 名册就从这份投影生成，远程 roster 传递的也是同一种格式。

**理由**：配置留在进程内，规格才能跨进程。两者混在一个类型里，要么配置序列化不了，要么规格带着一堆活对象。

---

## 模型层

### 为什么 Message 以 OpenAI 线格式为规范格式？

框架内部流转的消息（`base/types.py` 的 `Message`）长成 OpenAI 的样子：
`role / content / tool_calls / tool_call_id`。Anthropic 适配器负责双向翻译。

**为什么不是自创中立格式？**

| | 自创中立格式 | OpenAI 形态为规范 |
|---|---|---|
| 第三方工具兼容 | 都要翻译 | LangChain/observability 工具链直接认 |
| 适配器数量 | 每家进出都要转 | OpenAI 直通，Anthropic 单点翻译 |
| 概念负担 | 多一层"框架语" | 全行业已熟悉这套词表 |

**代价与对策**：中立格式的座位问题真实存在——Anthropic 的 thinking
块在 OpenAI 形态里没有位置。对策是 `Message.thinking` 扩展键：原始块
（含签名）挂在 assistant 消息上原样往返，不翻译、不投影；`reasoning_content`
只是它的纯文本投影视图，给展示和记账用。

> 注意一个不对称：**消息格式以 OpenAI 为规范，停止原因以 Anthropic
> 为规范**（`kernel/types.py` 的 `StopReason` 注释明说）。两边各取事实
> 上的业界 lingua franca，而不是单选一家。

### 为什么 thinking 块原样往返而不是只存纯文本？

Anthropic 的规则：工具调用续轮必须把最后一条 assistant 消息的 thinking
块**带签名**重发，否则 API 直接拒绝。纯文本（`reasoning_content`）丢了
签名，等于丢了重发的资格。所以存储的是原始块，文本只是视图。

启用方式：`LLMConfig(thinking_budget_tokens=2048)`。适配器会同时停发
`temperature`（thinking 期间 API 把它钉在 1）并保证 `max_tokens` 大于预算。

---

## 预算层

### 为什么是四轴而不是一轴？

很多框架只有 `max_iterations`。prodagent 有 turns/seconds/tokens/cost 四轴。

**为什么不能只靠 turns？**
- 20 轮可能只花 $0.01，也可能花 $50（每轮 100k token）
- 轮数不能反映真实成本

**为什么不能只靠 cost？**
- 死循环时每轮可能只花很少钱，但永远不停
- 需要 turns 轴做兜底

**为什么不能只靠 tokens？**
- 不同模型的 token 价格差异巨大（GPT-4 vs 本地模型）
- 相同 token 数，成本可能差 10 倍

**为什么不能只靠 seconds？**
- API 可能很快返回但烧了很多 token
- 需要 tokens/cost 轴控制

**结论**：四轴是"互为兜底"的关系。任何一轴都可能被绕过，但四轴同时生效就很难失控。

---

### 为什么 cache_read 不计入 token 预算？

```python
billable_tokens = total_tokens - cache_read_tokens
```

**理由**：
- Anthropic cache_read 只收 10% 费用，OpenAI 收 50%
- 如果全额计入预算，会出现"用了缓存反而更快耗尽预算"的反直觉行为
- 用户开缓存是为了省钱，不应该因为省钱而被预算限制

**但 cache_write 计入**，因为 cache_write 是正常计费甚至有溢价（Anthropic 1.25x）。

---

### 为什么时间预算用硬超时而不是事后检查？

```python
# 错误做法：调用完了再看超没超
response = await llm.complete(...)
if time_elapsed > max_seconds:
    raise BudgetExceeded()

# prodagent 的做法：到点直接掐断
response = await asyncio.wait_for(coro, timeout=remaining_time)
```

**理由**：事后检查时，钱已经花了、时间已经过了。硬超时是"防止损失扩大"，事后检查是"记录损失"。生产环境需要前者。

---

## 恢复层

### 为什么保存完整状态而不是事件溯源？

| | 完整状态快照 | 事件溯源（Event Sourcing） |
|---|---|---|
| 恢复速度 | 快（直接加载对象） | 慢（重放所有事件） |
| 存储大小 | 大（每轮存完整状态） | 小（只存增量事件） |
| 可调试性 | 高（直接看状态对象） | 低（需要重放才能看状态） |
| 实现复杂度 | 低 | 高 |
| 时间旅行 | 需要版本历史 | 天然支持 |

**选择快照的理由**：
- Agent Run 的状态对象不大（通常几十 KB）
- 恢复速度比存储成本重要
- 简单可靠比功能丰富重要
- 需要版本历史时，CheckpointStore 的 EXTENDED 能力可以提供（fork/list_versions）

---

### 为什么用乐观并发而不是分布式锁？

**乐观并发**：读版本 → 改 → 写时检查版本，不一致就报错重试。
**分布式锁**：先加锁 → 改 → 释放锁。

**选择乐观并发的理由**：
- Agent Run 的写入冲突概率极低（一个 Run 通常只有一个执行者）
- 分布式锁难实现、易死锁、需要维护锁服务
- 乐观并发不需要额外基础设施，数据库的原子操作就能支持
- 冲突时的处理很简单：重新加载最新状态，重试一次

---

## 工具层

### 为什么工具参数错误返回 ToolResult 而不是抛异常？

```python
# 错误做法：抛异常，打断整个循环
if invalid_params:
    raise ValueError("参数错误")

# prodagent 的做法：返回结构化错误，让模型自己修正
return ToolResult.from_error(
    ToolError.from_reason(
        ErrorReason.FORMAT_ERROR,
        message="参数错误",
        hint="有效参数是: [...]"
    )
)
```

**理由**：
- 模型会犯错（传错参数、调用不存在的工具），这是正常现象，不是异常
- 返回结构化错误 + 修正建议，模型可以在下一轮自己修正
- 抛异常会打断循环，用户看到的是"程序崩了"而不是"Agent 在学习修正"
- 这符合"Agent 是自主决策者"的设计哲学——给它反馈，让它调整

---

### 为什么只读工具可以并行，写工具必须串行？

```python
# ToolDispatcher 的策略
if all(tool.meta.is_readonly for tool in batch):
    await asyncio.gather(*[execute(t) for t in batch])  # 并行
else:
    for tool in batch:
        await execute(tool)  # 串行
```

**理由**：
- 只读工具（搜索、查询、读取）没有副作用，并行安全
- 写工具（发送、删除、修改）可能有依赖关系和副作用，并行可能导致竞态
- 这是"安全优先"的默认策略——用户可以通过自定义 dispatcher 覆盖

---

## 多 Agent 层

### 为什么单 Agent 是默认？

**理由**：
- 多 Agent 更贵（多个 Agent 各消耗 token）
- 多 Agent 更慢（通信 overhead）
- 多 Agent 更难调试（问题出在哪？）
- 多 Agent 可能死循环（A 推 B，B 推 A）

**prodagent 的哲学**：先把单 Agent 的上下文管理做好（记忆、压缩、技能），实在搞不定再拆多 Agent。很多"需要多 Agent"的场景，其实是单 Agent 的上下文管理没做好。

---

### 为什么所有拓扑共用一个消息平面？

五种拓扑（spawn/peer/ensemble/board/queue）的通信都走 Crossing 管道。

**理由**：
- "不丢不重不乱序"是所有多 Agent 系统的共同需求
- 在一个地方解决五道关卡（去重/契约/截断/Gate/死信），比在五个拓扑里各写一遍更可靠
- 统一的可观测性——所有消息都有 trace，不需要为每种拓扑单独加埋点

---

## 测试层

### 为什么全部测试用 FakeLLM 离线跑？

```python
# conftest.py
os.environ["USE_FAKE_LLM"] = "1"
# 注释：the whole suite runs offline, zero API keys, zero services.
# That is a feature; keep it true.
```

**理由**：
- 真实 API 有延迟、有成本、有 rate limit、可能 flaky
- FakeLLM 可以精确控制每一轮的输出，测试确定性场景
- 1,300+ 个测试如果连真实 API，跑一次可能要几小时、花几十美元
- 离线测试可以在 CI 里频繁跑，每次 commit 都验证

**FakeLLM 不是"简化版"**，它可以精确模拟：
- 多轮工具调用序列
- 流式输出
- 缓存命中
- 错误响应
- 超时

---

### 为什么测试命名这么长？

```
test_spawned_child_trips_on_spend_already_committed_by_a_sibling
test_blackboard_buzz_in_lock_race
test_plan_crash_recovery_e2e
test_work_queue_lease_timeout_requeue
```

**理由**：测试名就是文档。看到测试名就知道它测什么场景、什么边界条件。不需要读测试代码就能理解测试意图。

---

## 事件层

### 为什么"步骤完成"有三个名字，却不合并成一种事件？

同一次步骤完成出现在三个地方：总线 `HookEvent.STEP_COMPLETED`（广播给挂钩能力）、事件流 `StepCompletedEvent`（发给流式消费者）、事件日志 `PlanEventType.STEP_COMPLETED`（落盘回放）。

不合并，因为三者要的东西不同：广播要 fan-out，流要携带活对象，日志要可重放——合成一种，每种都做不好。

但拼写必须一一对应，否则每读一处就得背一次暗号。约定：**总线用小写点分，流事件用 PascalCase 类名，日志用 PascalCase 字符串值，词干相同**：

| 这件事 | 总线 | 事件流 | 日志 |
|------|------|--------|------|
| step 开始 | `step.started` | `StepStartedEvent` | `StepStarted` |
| step 完成 | `step.completed` | `StepCompletedEvent` | `StepCompleted` |
| step 失败 | `step.failed` | `StepFailedEvent` | `StepFailed` |
| run 完成 | `run.complete` | `RunCompletedEvent` | `RunCompleted` |
| run 失败 | `run.failed` | `RunFailedEvent` | `RunFailed` |
| turn 完成（REACTIVE） | — | — | `TurnCompleted` |

事件流事件和它的 JSON 编解码（`event_to_wire` / `event_from_wire`）定义在 `ports/agent_events.py`；`kernel/types.py` 重导出，消费方保持单一导入站点。

---

## 代码定位

| 决策 | 相关源码 |
|------|---------|
| Protocol 端口 | `ports/` |
| kernel 拆分 | `kernel/` `runtime/` |
| 四轴预算 | `kernel/budget.py` |
| 乐观并发 | `ports/persistence.py` |
| 工具错误处理 | `tooling/base.py` |
| 消息平面 | `coordination/messaging/` |
| FakeLLM | `llm/fake.py` |
| 消息格式宪法 | `base/types.py` `kernel/types.py` |
| thinking 往返 | `llm/anthropic_adapter.py` |
| 结算信封 | `kernel/budget.py` |
| Transport 端口 | `ports/messaging.py` |
| RunnerPort | `ports/execution.py` |
| Activation 排班 | `ports/execution.py` |
| AgentSpec 投影 | `ports/execution.py` |
| 舞台工具 | `coordination/infra/stage_tools.py` |
| 账本工厂 open_ledger | `kernel/budget.py` |
| 事件编解码 | `ports/agent_events.py` |

---

## 下一步

- 想查术语？→ [术语表 →](glossary.md)
- 想看 API？→ [API 参考 →](reference.md)
- 想回到首页？→ [学习路线 →](index.md)
