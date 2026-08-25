# 术语表

> 快速查阅 prodagent 中的核心概念。按字母排序。

---

## A

### Agent
有身份（name）、系统提示（system_prompt）、工具集（tools）和执行模式（mode）的实体。Agent 本身是无状态的配置对象，状态属于 Run。

### Approval（审批）
HIGH/CRITICAL 副作用工具执行前的人工确认环节。审批挂起时 Run 进入 SUSPENDED 状态，通过后直接执行 pending_tool_call，不重新问 LLM。

### Assembly（上下文组装）
每轮模型调用前，ContextManager 将记忆召回、上下文压缩、技能注入等处理后的 (system, messages) 传给模型。模型看到的不是原始消息列表。

---

## B

### Backend（后端）
端口（Port）的具体实现。如 FileCheckpointStore 实现 CheckpointStore，OpenAIClient 实现 LLMClient。默认用 file/memory，生产可换 postgres/redis。

### Budget（预算）
限制 Agent 资源消耗的四轴硬上限：turns（轮数）、seconds（时间）、tokens（billable token）、cost（美元）。任一触顶即抛 BudgetExceeded。

### BudgetLedger（预算账本）
多 Agent 共享的预算账本。支持 reserve（预占）/ commit（实扣）/ release（退还）三阶段，asyncio.Lock 保护并发。

---

## C

### Checkpoint（检查点）
AgentRun 状态的持久化快照。每轮 Step 结束后保存，支持乐观并发控制（expected_version）。崩溃恢复时加载最新 checkpoint 继续执行。

### Cognition（认知层）
prodagent 的包之一，包含上下文压缩（context）和记忆系统（memory）。负责"模型看到什么"。

### Compression（上下文压缩）
token 超阈值时按五级策略压缩历史消息。每级有明确的语义损失边界，关键约束（系统提示、当前任务）不被压缩。

### Coordination（协作层）
prodagent 的包之一，包含多 Agent 协作的五种原语：spawn、peer、ensemble、blackboard、work_queue，以及统一消息平面。

### Crossing（消息平面）
多 Agent 通信的统一管道。五道关卡：去重 → 契约校验 → 安全 → 审计 → 死信。保证消息不丢、不重、不乱序。

---

## D

### DAG（有向无环图）
PLAN_FIRST 和 Workflow 模式使用的任务依赖图。节点是步骤，边是依赖关系。支持断点续跑——已完成的节点不重复执行。

### Dead Letter（死信）
处理失败的消息/任务进入死信队列，不阻塞其他任务。可事后人工处理。

---

## E

### Ensemble（投票/集合）
多 Agent 协作原语之一。多个 Agent 共享会话，各出意见，由聚合策略（RoundRobin/Moderated/FreeForAll）裁决。适合评审、对抗、多意见场景。

### Event Bus（事件总线）
内核的 HookRegistry，在关键节点（TURN_START、LLM_REQUEST、THINK、TOKEN_UPDATE、LOOP_START/END 等）触发事件。可观测性、审计等通过订阅事件实现。

---

## F

### FakeLLM
内置的可复现模型适配器。通过预设响应序列或 script 装饰器精确控制每一轮输出。1,182 个测试全部用 FakeLLM，零 API key、零网络。

---

## H

### HITL（Human-in-the-Loop）
人工介入环节。主要指 HIGH 副作用工具的审批门。prodagent 的 HITL 设计：挂起 → 审批通过直接执行 / 审批拒绝增量重规划。

### Hooks（横切钩子）
prodagent 的包之一，包含审批（approval）、权限（authorization）、可观测（observability）、审计（audit）等横切关注点。通过事件总线注入，不侵入核心循环。

---

## K

### Kernel（内核）
prodagent 的包之一，最核心的循环逻辑。包含 loop（循环策略）、step（一轮原子执行）、budget（预算）、bus（事件总线）、state（运行状态）、types（类型定义）。

---

## L

### Leaf Executor（叶子执行器）
DAG 中单个步骤的执行器。实现 LeafExecutor Protocol，可自定义步骤的执行逻辑。

### LLMClient
模型调用的端口（Protocol）。所有模型适配器（OpenAI/Anthropic/Fake）都实现这个接口。核心循环只依赖这个端口，不依赖具体 SDK。

---

## M

### Memory（记忆）
四通道记忆系统：规则（rules）、实体（entities）、精确（exact）、语义（semantic）。并行召回 + 冲突裁决 + 遗忘曲线。不是只有向量检索。

### Message（消息）
对话历史中的一条消息，OpenAI 格式的字典：{"role": "user|assistant|tool", "content": "..."}。不封装成类，与 API 格式直接对齐。

---

## P

### Peer（接力）
多 Agent 协作原语之一。顺序执行，前一个 Agent 的输出通过 HandoffPacket 传给后一个。适合流水线场景。

### Plan-First（先规划后执行）
执行模式之一。先让模型输出 DAG 计划，再按依赖关系执行。执行中可增量重规划。适合有明确步骤的复杂任务。

### Port（端口）
六边形架构中的接口定义，用 @runtime_checkable Protocol 实现。核心只依赖端口，不依赖具体实现。prodagent 有 14 个端口。

### Progress Guard（进度守卫）
死循环检测器。通过 fingerprint 窗口比对最近 N 轮的工具调用模式，发现重复或停滞时抛 InfiniteLoopDetected。

---

## R

### Reactive（响应式）
执行模式之一。每轮：想一步 → 做一步 → 看结果 → 再想。不预先规划，适合探索式任务。是默认模式。

### Run（运行）
一次任务执行的完整生命周期。包含 run_id、task、state、messages、metrics、pending_tool_call 等。可序列化，是 checkpoint 和恢复的单位。

### RunState（运行状态）
Run 的状态枚举：RUNNING、COMPLETED、SUSPENDED、FAILED。状态转换由循环控制。

---

## S

### SAFETY_NET_BUDGET
默认预算：max_turns=20, max_seconds=120, max_tokens=100k, max_cost=$1.0。偏保守，设计哲学是"无人值守的任务快速失败而非慢慢烧钱"。

### SideEffectLevel（副作用等级）
工具的副作用分级：READONLY（只读，可并行）、LOW（低副作用）、HIGH（高副作用，需审批）、CRITICAL（关键操作，需二次确认）。

### Skill（技能）
成功 run 蒸馏出的 runbook。下次遇到同类任务时召回，注入 system prompt。越用越稳。

### Spawn（委派）
多 Agent 协作原语之一。父 Agent 派生子 Agent 执行子任务，子完成后返回结果。父子关系明确，预算通过 BudgetLedger 共享。

### Step（步骤）
代理的原子单位：一次模型调用 + 至多一轮工具执行。包含 _prepare → _call_llm → _account → _end_turn → run_batch 的完整流程。是可恢复的最小单位。

---

## T

### Tool（工具）
Agent 可调用的函数。用 @tool 装饰器定义，有 name、meta（副作用等级等）、schema（JSON Schema）。参数用 Pydantic TypeAdapter 校验。

### ToolDispatcher（工具调度器）
工具调用的执行器。负责只读并行/写串行、权限校验、审批门、执行、结果写回。

### Turn（轮次）
模型的一次输出。一个 Step 包含一个 Turn。Turn 可能包含文本输出和/或工具调用。

---

## W

### WorkQueue（工作队列）
多 Agent 协作原语之一。任务分发到队列，Worker 领取执行。支持租约（超时重入队）和死信（多次失败存档）。适合大量独立小任务。

### Workflow（工作流）
执行模式之一。完全预定义的静态 DAG，模型不参与规划。适合确定性流程、合规要求固定路径的场景。

---

## 回到

- [学习路线首页 →](tour/index.md)
- [设计取舍 →](decisions.md)
- [API 参考 →](reference.md)
