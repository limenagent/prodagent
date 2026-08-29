# 术语表

> 按主题分类的核心术语。遇到不认识的词先来这里查。

---

## 核心抽象

| 术语 | 定义 |
|------|------|
| **Agent** | 有身份（name）、系统提示、工具集的实体。无状态配置对象，可并发跑多个 Run。 |
| **Run** | 一次任务执行的完整生命周期。有状态（RUNNING/COMPLETED/SUSPENDED/FAILED），可序列化。 |
| **Step** | 代理的原子单位：一次模型调用 + 至多一轮工具执行。可恢复的最小单位。 |
| **Turn** | 模型的一次输出。一个 Step 包含一个 Turn。 |
| **Message** | 对话历史中的一条消息（OpenAI 格式 dict）。 |
| **ToolCall** | 模型请求调用工具的指令（name + params + call_id）。 |
| **ToolResult** | 工具执行后的返回（outcome + content/error）。 |
| **ToolOutcome** | 工具执行结局：OK/RETRY/ABORT/BLOCKED/SUSPENDED/HANDOFF。 |
| **StopReason** | 模型停止原因：END_TURN/TOOL_USE/MAX_TOKENS/CONTENT_FILTER。 |
| **AgentSpec** | Agent 的可序列化投影：名字、提示、模式、预算、工具 schema、子与同伴规格。`Agent.spec()` 生成，跨进程传递用。 |

---

## 执行模式

| 术语 | 定义 |
|------|------|
| **ExecutionMode.REACTIVE** | 边走边想模式。每轮模型自己决定下一步。默认模式。 |
| **ExecutionMode.PLAN_FIRST** | 先规划后执行模式。模型先输出 DAG，再按依赖执行。 |
| **Workflow** | 手写确定性 DAG 的构建器（不是执行模式）。通过 `workflow=` 参数传给 Agent。 |
| **Plan** | 编译后的 DAG，包含多个 PlanStep 和依赖关系。 |
| **PlanStep** | DAG 中的一个节点（id + action + params + depends_on）。 |

---

## 工具系统

| 术语 | 定义 |
|------|------|
| **Tool** | 工具的统一接口（Protocol）。 |
| **FunctionTool** | 用 `@tool` 装饰器从 Python 函数创建的工具。 |
| **ToolMeta** | 工具的静态元数据：name, is_readonly, side_effect_level, enforced_idempotent, timeout_seconds, domain, max_result_chars。 |
| **SideEffectLevel** | 副作用等级：LOW/MEDIUM/HIGH。没有 CRITICAL，没有 READONLY（只读用 is_readonly bool 表示）。 |
| **is_readonly** | ToolMeta 的布尔字段。True 表示只读，可并行执行。 |
| **ToolDispatcher** | 工具调度器：只读并行/写串行、权限检查、审批门、超时控制。 |

---

## 预算与压缩

| 术语 | 定义 |
|------|------|
| **HardBudget** | 四轴预算：max_turns/max_seconds/max_tokens/max_cost_usd。 |
| **BudgetLedger** | 多 Agent 共享预算账本，三阶段记账（reserve/commit/release）。 |
| **billable_tokens** | 计入预算的 token = total - cache_read（缓存命中几乎不花钱）。 |
| **CompressionLevel** | 压缩级别：NONE/TOOL_COMPRESS/HISTORY_SUMMARY/TOPIC_SUMMARY/EMERGENCY。 |
| **ContextBudget** | 上下文窗口的分层预算：L0(system 8%)/L1(state 15%)/L2(memory 35%)/L3(history 42%)。 |
| **Spill** | 超长工具结果溢出到外部存储，消息中只保留摘要。 |

---

## 记忆系统

| 术语 | 定义 |
|------|------|
| **MemoryManager** | 记忆管理器，协调分类、存储、召回、冲突裁决、遗忘。 |
| **RuleChannel** | 规则通道：高置信度、永久有效（"用户说用中文"）。 |
| **EntityChannel** | 实体通道：关于实体的属性（"用户的职位是工程师"）。 |
| **ExactChannel** | 精确通道：精确匹配的事实。 |
| **SemanticChannel** | 语义通道：向量相似度召回的经验。 |
| **MemoryType** | EPISODIC（情景，有 TTL）/SEMANTIC（语义，长期）/PROCEDURAL（程序，技能）。 |
| **SkillCard** | 技能卡片：name + description + version + tags，始终在上下文中。 |
| **SkillRegistry** | 技能注册表，管理技能的加载、评分、退役。 |
| **SkillSynthesizer** | 从成功会话中蒸馏可复用技能。 |

---

## 多 Agent 协作

| 术语 | 定义 |
|------|------|
| **Spawn** | 垂直委派：父 Agent 通过 `agents=[...]` 配置，模型调用 spawn_agent 工具派任务给子 Agent。 |
| **RunnerPort** | 激活一个 agent 执行一次 run 的端口（spawn 子任务、舞台成员发言共用）。进程内实现是 RunLoop。 |
| **AgentActivation** | 一次激活的入参：agent、任务、run_id、账本；带 session_id 就是成员会话轮次。 |
| **Activation** | 舞台拓扑的排班单：这轮哪些成员上、serial / concurrent / single_winner 三种派发。 |
| **HandoffActivation** | 接力链下一跳：peer 名、任务、run_id。relay 填写，驱动方照此 fork。 |
| **run_ensemble / run_work_queue** | 舞台工具：带名字的 EnsembleSpec / WorkQueueSpec 声明在 AgentConfig 上即生成，模型自己召集评审或派活。 |
| **AgentWorkMember** | 把 Agent 适配成队列 Worker：认领一个 item 就是一次会话激活。 |
| **Peer** | 水平接力：通过 `peers=[...]` 配置，模型调用 handoff_to_X 工具把任务交给下一个 Agent。 |
| **HandoffPacket** | 接力时传递的上下文包（任务、输入引用、约束）。 |
| **Ensemble** | 舞台拓扑：多个 Agent 共享 Floor 轮流发言（RoundRobin/Moderated/FreeForAll）。 |
| **Blackboard** | 舞台拓扑：共享版本化 Board，Trigger 声明式触发 Expert。 |
| **WorkQueue** | 舞台拓扑：拉模式任务分发，租约 + 死信。 |
| **Crossing** | 消息信封，携带 direction/kind/from/to/payload/trace_id。 |
| **Pipeline** | 消息拦截器链：去重 → 契约校验 → 截断 → Gate 检查 → 死信。 |
| **Transport** | 消息平面端口（进程内实现，可替换为 Redis/NATS）。 |
| **DeadLetterStore** | 死信队列端口（memory/redis 后端）。 |

---

## 持久化与恢复

| 术语 | 定义 |
| **游标（cursor）** | 执行器在 `AgentRun.cursors` 里的恢复进度，plan / reactive 各一格；新执行模式加一格，不加 Run 字段。 |
|------|------|
| **CheckpointStore** | Run 状态快照端口（file/postgres 后端）。支持 save/load/list_run_ids/fork/list_versions。 |
| **SessionStore** | 跨 Run 会话持久化端口（file/postgres 后端）。 |
| **EventLog** | 追加写入的事件日志端口（file/postgres 后端），支持事件溯源。 |
| **pending_tool_call** | 审批挂起时保存的工具调用，恢复时直接执行不重新问模型。 |
| **pending_handoff** | 多 Agent 接力的待交接包（PendingHandoff 类型）。 |
| **IdempotencyKey** | 幂等键，防止工具重复执行（基于 ToolCall.params_hash）。 |

---

## 可观测与治理

| 术语 | 定义 |
|------|------|
| **HookEvent** | 事件枚举（29 个），如 session.start/loop.start/tool.call/llm.think/run.complete。 |
| **Gate** | 拦截点枚举（9 个），如 TOOL_CALL/APPROVAL_REQUEST/AGENT_HANDOFF。 |
| **HookRegistry** | 三协议总线：fire(OBSERVE 并发)/check_blocking(VETO 串行)/collect(GATHER 并发)。 |
| **AgentSpan** | 决策快照（span_id/run_id/event/data/duration_ms/status）。 |
| **SpanExporter** | Span 导出端口（file/postgres 后端，可对接 OTel）。 |
| **ApprovalStore** | 审批请求持久化端口（目前仅 memory 后端）。 |
| **ApprovalGate** | 审批门：HIGH 副作用工具执行前挂起等人。 |
| **BlockingResult** | Gate checker 的返回值，blocked=True 表示拦截。 |

---

## 架构

| 术语 | 定义 |
|------|------|
| **端口（Port）** | Protocol 接口，定义核心与外部世界的契约。可替换端口共 17 个（`ports/` 目录下一共 20 个 Protocol，另 3 个是内部协作契约 StageStore / ActivationPolicy / SpendView）；AgentSpec、事件编解码、Activation 这些跨进程传递的定义也在这层。 |
| **适配器（Adapter）** | 端口的具体实现（file/postgres/redis/neo4j 等）。 |
| **组装根（Composition Root）** | 全仓库唯一决定"一个 Agent 由哪些零件拼成"的地方，即 `runtime/compose.py`；也是唯一读取 profile、唯一被允许点名 coordination 的 runtime 文件。 |
| **三个插座** | 扩展能力的全部三种入口：端口替换（实现 Protocol）、总线挂载（注册 hook）、执行器替换（实现 LeafExecutor）。 |
| **HookBundle（墨盒）** | 自装配能力包，只暴露一个 `attach(agent, fw, registry)`；记忆/学习/可观测/安全各是一个墨盒，组装根按 profile 清单挂载。 |
| **懒加载（lazy_package）** | 基于 PEP 562 模块级 `__getattr__`，符号第一次被访问时才 import 对应模块，让 `import prodagent` 保持轻量。 |
| **原子写** | 先写临时文件再 `os.replace` 改名，读者只会看到完整旧文件或完整新文件，不会看到写一半的撕裂内容。 |
| **full jitter 退避** | 重试间隔在 `[0, 基数×2^n]` 随机取，打散同时失败的多个 Agent，避免"惊群"再次压垮上游。 |
| **MCP** | Model Context Protocol，外部工具经 stdio/HTTP 接入的标准；bridge 把远端工具适配成本地 FunctionTool，走同一条调度流水线。 |
| **BackendConfig** | 后端选择配置，用字符串字面量约束可选值。 |
| **bare()** | 裸核 profile：无持久化、无审批、无缓存、无压缩。 |
| **production()** | 生产 profile：file 后端 + 压缩 + spill + 审批门 + 缓存。 |
| **LLMConfig** | 模型配置（model/temperature/max_tokens/定价/缓存参数），定义在 ports/llm.py。 |
| **LLMClient** | 模型调用端口，只有一个 complete() 方法。 |
| **FakeLLMAdapter** | 假模型，预设响应序列，用于测试和学习。 |
