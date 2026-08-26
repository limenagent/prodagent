# 心智模型：一次 `chat()` 调用的完整生命周期

> 跟着一次 `agent.chat("任务")` 调用，走完 prodagent 的完整链路。
>
> 读完你能在白板上画出整个运行时，说清每一层做了什么、为什么这么做、数据怎么流动。
>
> 这是比"七站之旅"更深入的源码对照——每个阶段都标注了具体的源码文件和关键函数。

---

## 全景：一次调用经过的 12 个阶段

```
agent.chat("任务")
  │
  ├─ 阶段 1：入口（runtime/agent.py）
  │    chat() → chat_stream() → _begin_chat_turn()
  │
  ├─ 阶段 2：装配（runtime/factory.py）
  │    LeafExecutorFactory.prepare()
  │    挂载 hooks → 解析工具 → 构建系统提示 → 构建上下文管理器 → 选择执行模式
  │
  ├─ 阶段 3：运行时驱动（runtime/runner.py）
  │    drive_stream() → collect_final_run()
  │
  ├─ 阶段 4：循环初始化（kernel/loop.py）
  │    ReactiveLoop.stream() → _resolve_run()
  │    新建 AgentRun 或从 checkpoint 恢复
  │
  ├─ 阶段 5：Step — think（kernel/step.py）
  │    Step._think() → _prepare() → _call_llm() → _account()
  │    预算检查 → 死循环检测 → 上下文组装 → LLM 调用 → 记账
  │
  ├─ 阶段 6：Step — decide（kernel/step.py）
  │    Step._end_turn()
  │    判断模型是结束了还是要调用工具
  │
  ├─ 阶段 7：Step — execute（tooling/dispatcher.py）
  │    ToolDispatcher.run_batch()
  │    只读并行 / 写串行 → 权限检查 → HITL 审批 → 工具执行 → 结果写回
  │
  ├─ 阶段 8：循环结算（kernel/loop.py）
  │    ReactiveLoop._record_turn() → 检查终止条件
  │    事件日志 + checkpoint → 判断是否继续循环
  │
  ├─ 阶段 9：多 Agent 协作（coordination/）
  │    如果触发了 spawn/peer/handoff → 进入协调层
  │    消息平面 → 子 Agent 运行 → 结果汇总
  │
  ├─ 阶段 10：终止（kernel/loop.py）
  │    COMPLETED / FAILED / SUSPENDED
  │    span 结束 → 终端事件 → 最后一次 checkpoint
  │
  ├─ 阶段 11：会话保存（runtime/agent.py）
  │    session.complete_turn() → store.save()
  │
  └─ 阶段 12：返回（runtime/runner.py）
       collect_final_run() → 返回 AgentRun
```

---

## 阶段 1：入口

**源码：** `src/prodagent/runtime/agent.py` — `Agent.chat()` / `Agent.chat_stream()`

### 做什么

```python
agent.chat("任务")
  → chat_stream(message, session_id, resume, mode)
    → _begin_chat_turn(message, sid, mode)   # 创建会话，分配 run_id
    → drive_stream(self, message, run_id, ...) # 进入运行时驱动
```

### 为什么这么设计

`chat()` 是同步入口，`chat_stream()` 是异步流式入口。`chat()` 内部调用 `chat_stream()` 然后收集最终结果——**只有一个执行路径**，不会出现"同步版和异步版行为不一致"的问题。

### 关键数据结构

```python
# ConversationSession — 多轮会话状态
@dataclass
class ConversationSession:
    session_id: str
    agent_id: str
    turns: list[TurnRecord]    # 历史回合
    version: int                # 乐观并发版本号
```

### 你可能没注意到的细节

- `resume=True` 需要显式 `session_id`——因为恢复需要知道从哪个会话恢复
- 如果 `message=None` 且不是 `resume`，会报错——因为 Agent 不知道要做什么
- 会话版本号用乐观并发——如果两个请求同时修改同一个会话，后写入的会失败

---

## 阶段 2：装配

**源码：** `src/prodagent/runtime/factory.py` — `LeafExecutorFactory.prepare()`

### 做什么

这是整个框架最"魔术"的地方——把一个声明式的 `Agent` 配置装配成一个可执行的循环。

```
LeafExecutorFactory.prepare(agent, ctx)
  │
  ├─ 1. agent.attach_default_hooks()
  │     构建 HookRegistry，挂载默认 hook bundles（审批/观测/记忆/学习）
  │     注册用户配置的 injectors/checkers/event_handlers/extensions
  │
  ├─ 2. agent.resolve_tools()
  │     合并内联工具 + tool_registry 工具 + MCP 工具
  │     如果开启了 spill，加一个 read_tool_result 工具
  │     加上协作工具（spawn_agent / handoff_to_peer 等）
  │
  ├─ 3. build_system_prompt()
  │     组装：# Agent 名称 → ## Context（系统提示）→ ## Hard Constraints → 技能 section
  │
  ├─ 4. build_context_manager()（如果开启了压缩）
  │     构建 ContextManager：L0-L3 分层预算 + 五级压缩管道 + spill 存储
  │
  └─ 5. 选择执行模式
        REACTIVE   → ReactiveLoop(llm, dispatcher, ...)
        PLAN_FIRST → PlanExecutor(planner, step_runner, ...)
        Workflow   → PlanExecutor（用预编译的 initial_plan）
```

### 为什么这么设计

装配逻辑集中在一个地方（`factory.py`），而不是散落在 `Agent.__init__` 和各个执行器里。这样：

- **新增执行模式** = 在 factory 里加一个分支，不改 Agent
- **新增工具来源** = 在 `resolve_tools()` 里加一步，不改执行器
- **测试装配逻辑** = 可以单独测试 factory，不需要构造完整 Agent

### 关键数据结构

```python
# RunContext — 一次运行的上下文（装配时构建，传递给执行器）
@dataclass
class RunContext:
    llm: LLMClient
    dispatcher: ToolDispatcher
    hooks: HookRegistry
    budget: HardBudget
    checkpoint: CheckpointStore | None
    event_log: EventLog | None
    spill_store: ToolResultSpillStore | None
    context_manager: ContextManager | None
    tool_assemblers: list[Callable]  # 协作工具装配器（spawn/peer/handoff）
```

### 你可能没注意到的细节

- `attach_default_hooks()` 是幂等的——多次调用只挂载一次。因为 `_hooks_wired` 标志位守卫。
- 工具合并按 name 去重——如果内联工具和 registry 工具有同名的，内联工具优先。
- MCP 工具是异步解析的——因为需要连接 MCP 服务器获取工具列表。

---

## 阶段 3：运行时驱动

**源码：** `src/prodagent/runtime/runner.py` — `drive_stream()` / `collect_final_run()`

### 做什么

```python
async def drive_stream(agent, message, *, run_id, forced_mode, initial_messages):
    ctx = await build_run_context(agent, run_id)  # 构建 RunContext
    executor = LeafExecutorFactory.prepare(agent, ctx)  # 装配执行器
    async for event in executor.stream(message, run_id=run_id):
        yield event  # 透传所有事件
```

`drive_stream` 是一个薄薄的适配层——它把 `Agent` + `RunContext` 变成一个可迭代的事件流，然后透传。

`collect_final_run` 消费事件流，找到终端事件（`RunCompletedEvent` / `RunFailedEvent` / `RunSuspendedEvent`），返回其中的 `AgentRun`。

### 为什么这么设计

运行时驱动层很薄，因为"驱动循环"的逻辑在执行器里（`ReactiveLoop` / `PlanExecutor`），不在 runner 里。runner 只负责：

1. 构建上下文
2. 装配执行器
3. 透传事件

这种设计让执行器可以独立测试——不需要 runner，直接构造 `ReactiveLoop` 就能测。

---

## 阶段 4：循环初始化

**源码：** `src/prodagent/kernel/loop.py` — `ReactiveLoop.stream()` / `_resolve_run()`

### 做什么

```python
async def stream(self, task, *, run_id, parent_run_id):
    run = await self._resolve_run(task, run_id=run_id, parent_run_id=parent_run_id)
    await self._begin_run_span(run, task)  # fire(LOOP_START)
    try:
        async for event in self._loop_events(run):
            yield event
    except BudgetExceeded as exc:
        yield await self._settle_terminated(run, exc)
    except InfiniteLoopDetected as exc:
        yield await self._settle_terminated(run, exc)
    except Exception as exc:
        await self._settle_unexpected(run, exc)
        raise
    else:
        await self._end_run_span(run)
        await self._record_terminal(run, RUN_COMPLETED or RUN_SUSPENDED)
    finally:
        if self._checkpoint_store is not None:
            await save_and_fire_checkpoint(self._checkpoint_store, run, self._hooks)
```

### _resolve_run：新建还是恢复？

```python
async def _resolve_run(self, task, *, run_id, parent_run_id):
    # 情况 1：有初始消息（chat 多轮）→ 用初始消息构建新 run
    if self._initial_messages is not None:
        return AgentRun(run_id=..., task=task, messages=list(self._initial_messages))

    # 情况 2：有 checkpoint_store 且指定了 run_id → 尝试恢复
    if self._checkpoint_store is not None and run_id:
        existing = await self._checkpoint_store.load(run_id)
        if existing is not None:
            if existing.state is not RunState.SUSPENDED:
                self._prune_unresolved_tool_uses(existing)
            existing.state = RunState.RUNNING
            existing.last_error = None
            return existing

    # 情况 3：默认 → 新建 run
    return self._init_run(task, run_id=run_id, parent_run_id=parent_run_id)
```

### 为什么这么设计

恢复逻辑集中在 `_resolve_run` 一个函数里。三种情况（新建/恢复/初始消息）清晰分离。

恢复时的关键处理：

1. **如果不是 SUSPENDED 状态**，要 `_prune_unresolved_tool_uses`——因为崩溃时可能有一个 assistant 消息带了 tool_calls 但没有对应的 tool_results。如果不修剪，恢复后模型会看到"我调用了工具但没结果"，可能困惑。
2. **状态重置为 RUNNING**——因为恢复后要继续运行。
3. **清除 last_error**——因为这是新的开始。

### 你可能没注意到的细节

- `monotonic_start` 在恢复时设为 `None`——因为单调时钟是进程相关的，跨进程恢复后无意义。恢复后 `elapsed_seconds()` 回退到 wall-clock 计算。
- 这意味着**恢复后的运行会把崩溃前的时间计入预算**——如果预算是 120 秒，崩溃前跑了 60 秒，恢复后只剩 60 秒。这是刻意设计的——预算是"总耗时"，不是"本次运行耗时"。

---

## 阶段 5：Step — think

**源码：** `src/prodagent/kernel/step.py` — `Step.run()` / `Step._think()`

这是整个框架的核心——一次 `think → decide → execute` 原子。

### _think 的三个子步骤

```
Step._think(run, system, tools)
  │
  ├─ _prepare(run, system)          ← 准备
  │     ├── check_budget(run)       ← 预算检查（思考前）
  │     ├── guard.check(run)        ← 死循环检测
  │     ├── fire(TURN_START)        ← 事件：回合开始
  │     ├── assembler(run)          ← 上下文组装（记忆召回 + 压缩）
  │     └── fire(LLM_REQUEST)       ← 事件：LLM 请求
  │
  ├─ _call_llm(run, system, messages, tools)  ← 调用模型
  │     ├── 计算剩余时间预算
  │     ├── asyncio.wait_for(coro, timeout=remaining)  ← 硬超时
  │     └── on_chunk 回调 → fire(THINK) + 收集 token 事件
  │
  └── _account(run, response)        ← 记账
        ├── run.metrics.turn_count += 1
        ├── run.add_tokens(response, cost_usd=...)  ← token/cost 记账
        ├── fire(TOKEN_UPDATE)       ← 事件：token 更新
        └── run.messages.append(assistant_message)  ← 追加 assistant 消息
```

### 预算检查的时机

预算检查在三个地方进行：

1. **`_prepare` 开头**——思考前检查。如果已经超预算，不调用模型。
2. **`_call_llm` 的硬超时**——调用中检查。如果时间预算耗尽，直接掐断。
3. **`run_batch` 之后**——工具执行后检查。如果工具执行导致超预算，不继续下一轮。

> **为什么检查这么多次？** 因为预算是"硬约束"——任何一个阶段都可能导致超预算。思考前检查防止"已经超了还调用模型"，硬超时防止"模型调用卡住"，工具后检查防止"工具执行烧了很多 token"。

### 死循环检测

```python
# kernel/progress.py
class ProgressMonitor:
    def check(self, run, *, new_call=None):
        # 基于 fingerprint 窗口检测
        # fingerprint = hash(tool_name + sorted(params))
        # 如果同一个 fingerprint 在窗口内出现超过 repeat_threshold 次 → InfiniteLoopDetected
        # 如果连续 stall_threshold 次没有新的工具调用 → InfiniteLoopDetected
```

两种死循环模式：

1. **重复循环**——模型反复调用同一个工具（同样的 name + 同样的 params）。比如反复调用 `search("weather")` 但每次结果都一样。
2. **停滞循环**——模型连续多轮没有调用工具，但也没有给出最终答案。比如反复说"让我想想"但不行动。

### 上下文组装

如果配置了 `ContextManager`，`assembler` 会调用 `ContextManager.prepare()`：

```
ContextManager.prepare(run)
  ├── 计算 L0-L3 分层预算
  ├── alloc_state_block()      ← L1：状态块（turn 数、失败次数）
  ├── alloc_memory_block()     ← L2：记忆召回（collect(CONTEXT_INJECTOR)）
  ├── build_invoked_skills_block()  ← 已调用技能的 runbook
  ├── compress_history()        ← L3：五级压缩（按 token 占比）
  ├── assemble_sandwich()       ← 组装：history + [MEMORY] + [SKILLS] + [STATE] + reminder
  └── enforce_total_budget()    ← 最终兜底：如果还超，截断到最后 2 条
```

### LLM 调用的细节

```python
# 硬超时
llm_timeout = max(0.1, self._budget.max_seconds - run.elapsed_seconds())
response = await asyncio.wait_for(coro, timeout=llm_timeout)

# 缓存边界
if self._cache_boundary is not None:
    llm_config = dataclasses.replace(
        llm_config, cache_boundary_index=self._cache_boundary()
    )
```

- **硬超时**：剩余时间 = 总预算 - 已用时间。如果剩余时间 < 0.1 秒，至少给 0.1 秒（防止超时为 0）。
- **缓存边界**：`cache_boundary_index` 是"历史消息的最后一条的索引"。Anthropic 的 prompt cache 会缓存这个索引之前的消息。每次调用时传入这个索引，让 Anthropic 知道哪些消息可以缓存。

### 记账的细节

```python
def add_tokens(self, response, *, cost_usd):
    self.metrics.input_tokens += response.input_tokens
    self.metrics.output_tokens += response.output_tokens
    self.metrics.cache_read_tokens += response.cache_read_tokens
    self.metrics.cache_write_tokens += response.cache_write_tokens
    self.metrics.cost_usd += cost_usd
```

- `cache_read_tokens` 单独记账——因为它不计入 token 预算（见设计哲学原则四）。
- `cost_usd` 由调用方预先计算——因为核心不依赖 LLM 定价表。`LLMConfig.cost_for_response()` 计算成本。

---

## 阶段 6：Step — decide

**源码：** `src/prodagent/kernel/step.py` — `Step._end_turn()`

### 做什么

```python
def _end_turn(self, run, response):
    # 如果模型停止原因不是 END_TURN 且有工具调用 → 继续执行工具
    if response.stop_reason != StopReason.END_TURN and response.tool_calls:
        return False  # 不结束，继续执行工具

    # 否则 → 运行完成
    run.state = RunState.COMPLETED
    run.final_output = response.content
    # 如果 content 为空，回溯找最后一条有 content 的 assistant 消息
    if not run.final_output:
        for msg in reversed(run.messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                run.final_output = msg["content"]
                break
    return True  # 结束
```

### 为什么这么设计

判断"模型是结束了还是要调用工具"的逻辑很简单：

- `stop_reason == END_TURN` 且没有工具调用 → 结束
- 否则 → 执行工具

但有一个边界情况：模型可能返回 `stop_reason == TOOL_USE` 但 `tool_calls` 为空（某些 provider 的奇怪行为）。这种情况下 `response.tool_calls` 为空，会被判定为"结束"。这是合理的降级——没有工具调用就无法执行工具，只能结束。

### final_output 的回溯

如果模型的最后一条响应 `content` 为空（比如模型只调用了工具没有文本），`final_output` 会回溯找最后一条有内容的 assistant 消息。这保证了 `run.final_output` 永远有值（除非整个对话都没有 assistant 文本）。

---

## 阶段 7：Step — execute

**源码：** `src/prodagent/tooling/dispatcher.py` — `ToolDispatcher.run_batch()`

### 做什么

```
run_batch(run, calls)
  │
  ├── 对每个 call：
  │     ├── progress.check(run, new_call=call)  ← 死循环检测
  │     ├── 如果 enforced_idempotent → 注入 idempotency_key
  │     ├── run.last_action = "name(params_keys)"
  │     ├── run.tool_history.append(call)
  │     └── yield ToolCallStartEvent
  │
  ├── 分类：
  │     只读工具 → readonly_calls（并行执行）
  │     写工具   → serial_calls（串行执行）
  │
  ├── 执行只读工具（并行，受信号量限制）：
  │     for call in readonly_calls:
  │       result = await dispatch(call)  ← 含权限检查 + 重试 + 执行
  │       _emit_result(result, call, run)  ← 结果写回 transcript
  │
  └── 执行写工具（串行）：
        for call in serial_calls:
          result = await dispatch_with_retry(call, run)
          _emit_result(result, call, run)
          如果 result 是 SUSPENDED 或 HANDOFF → 提前结束 batch
```

### 只读并行 / 写串行

```python
if self.is_readonly(call.name):
    readonly_calls.append(call)
else:
    serial_calls.append(call)

# 只读工具：并行执行，受信号量限制
semaphore = asyncio.Semaphore(readonly_concurrency)  # 默认 8
async def _dispatch_with_cap(call):
    async with semaphore:
        return await self.dispatch(call, run_id=run.run_id)
raw = await asyncio.gather(*[_dispatch_with_cap(c) for c in readonly_calls])

# 写工具：串行执行
for call in serial_calls:
    result = await self.dispatch_with_retry(call, run)
```

**为什么只读可以并行？** 因为只读工具没有副作用，并行不会导致竞态条件。
**为什么写必须串行？** 因为写工具可能有依赖关系（A 的输出是 B 的输入），并行可能导致竞态。

> **注意：** 这是默认策略。如果你的写工具之间没有依赖，可以自定义 dispatcher 实现并行。但默认是安全优先。

### dispatch：单次工具调用的完整链路

```
dispatch(call, run_id)
  │
  ├── 查找工具：tool_map.get(call.name)
  │     如果找不到 → 返回 ToolError(TOOL_NOT_AVAILABLE)
  │
  ├── 参数校验：工具的 JSON Schema 校验 call.params
  │     如果校验失败 → 返回 ToolError(FORMAT_ERROR, hint="有效参数是...")
  │
  ├── 权限检查：bus.check_blocking(Gate.TOOL_CALL, ...)
  │     如果被拦截 → 返回 ToolResult.blocked_by(reason=...)
  │
  ├── HITL 审批：如果 side_effect_level == HIGH
  │     挂起运行，等待人工审批
  │     返回 ToolResult.suspended(approval_request_id=...)
  │
  ├── 执行工具函数（带超时）：
  │     asyncio.wait_for(tool.fn(**params), timeout=meta.timeout_seconds)
  │
  ├── 结果规范化：coerce_result(result) → ToolResult
  │
  └── 返回 ToolResult
```

### 熔断器

```python
# tooling/reliability/circuit_breaker.py
class CircuitBreaker:
    # 滑动窗口统计失败率
    # 如果失败率超过阈值 → 打开熔断器（快速失败，不调用工具）
    # 一段时间后 → 半开状态（允许少量请求试探）
    # 如果试探成功 → 关闭熔断器（恢复正常）
```

熔断器防止"工具持续失败但 Agent 反复调用"的情况。如果一个工具连续失败，熔断器会打开，后续调用直接快速失败（返回错误），而不是真正调用工具。

### _emit_result：结果写回 transcript

```python
def _emit_result(self, result, call, run, deferred_injections, emitted):
    # 如果是 SUSPENDED → park_for_approval，提前结束 batch
    if result.outcome is ToolOutcome.SUSPENDED:
        run.park_for_approval(call, result.approval_request_id)
        return True  # 提前结束

    # 如果是 HANDOFF → park_handoff，提前结束 batch
    if result.outcome is ToolOutcome.HANDOFF:
        run.park_handoff(result.handoff)
        return True

    # 正常结果 → 写回 transcript
    wire = result.to_wire()
    run.messages.append(self.build_tool_message(wire, call, run))
    return False
```

### batch 提前结束的处理

如果 batch 中有一个工具返回 SUSPENDED 或 HANDOFF，batch 会提前结束。但此时 assistant 消息可能携带了多个 tool_calls（模型在一轮中请求了多个工具），只有部分工具得到了结果。

为了保持 transcript 的 wire-valid（provider 要求每个 tool_use 都有对应的 tool_result），未执行的工具会被标记为 "skipped"：

```python
def _balance_batch(self, run, calls, emitted, *, keep):
    for call in calls:
        if call is keep or call.call_id in emitted:
            continue
        run.messages.append(Message(
            role="tool",
            tool_call_id=call.call_id,
            content=f"skipped: run ended before '{call.name}' was dispatched",
        ))
```

---

## 阶段 8：循环结算

**源码：** `src/prodagent/kernel/loop.py` — `ReactiveLoop._record_turn()` / 终止检查

### 做什么

每一轮 Step 完成后：

```python
# _loop_events 中的循环
while True:
    async for event in self._step.run(run, ...):
        yield event
    await self._record_turn(run)  # 事件日志 + checkpoint

    # 终止检查
    if run.pending_handoff is not None:
        yield RunCompletedEvent(run=run)
        return
    if run.state is RunState.COMPLETED:
        yield RunCompletedEvent(run=run)
        return
    if run.state is RunState.SUSPENDED:
        yield RunSuspendedEvent(run=run)
        return
```

### _record_turn

```python
async def _record_turn(self, run):
    # 只有同时配置了 event_log 和 checkpoint_store 才记录
    if self._event_log is None or self._checkpoint_store is None:
        return
    # 追加 TURN_COMPLETED 事件（乐观并发）
    seq = await self._event_log.append(
        Event.make(RunEventType.TURN_COMPLETED, stream_id=run.run_id),
        expected_seq=run.last_event_seq,
    )
    run.last_event_seq = seq
    # 保存 checkpoint
    await save_and_fire_checkpoint(self._checkpoint_store, run, self._hooks)
```

> **注意：** `_record_turn` 是"双写"——同时写事件日志和 checkpoint。事件日志是增量的（每轮追加一条），checkpoint 是全量的（保存整个 AgentRun）。两者配合实现崩溃恢复。

### 终止条件的优先级

终止检查按优先级排列：

1. **pending_handoff**——如果有待处理的接力，运行完成（控制权转移给 peer）
2. **state == COMPLETED**——模型给出了最终答案
3. **state == SUSPENDED**——等待 HITL 审批

> **为什么 handoff 优先于 COMPLETED？** 因为 handoff 时 `run.state` 也会被设为 COMPLETED（见 `state.py` 的 `park_handoff`）。但 handoff 的语义是"控制权转移"，不是"任务完成"。所以先检查 `pending_handoff`，确保 handoff 被正确处理（触发 relay），而不是被当作普通完成。

---

## 阶段 9：多 Agent 协作

**源码：** `src/prodagent/coordination/`

如果在工具执行阶段触发了 `spawn_agent` 或 `handoff_to_peer`，会进入协调层。

### spawn（垂直委派）

```
spawn_agent(name="researcher", task="搜索资料")
  │
  ├── 构建 packet（HandoffPacket）
  ├── DOWNSTREAM：dispatch_transport
  │     去重 → 契约 → 安全 → 审计 → 死信
  │
  ├── fork_as_spawn()——从父 Agent 派生一个子 Agent
  │     继承：llm, hooks, framework_config, budget, checkpoint
  │     隔离：messages, metrics, state
  │
  ├── BudgetLedger.reserve()——预留预算
  │
  ├── 子 Agent 运行（独立的 chat() 调用）
  │
  ├── BudgetLedger.commit()——结算实际花费
  │
  ├── UPSTREAM：result_transport
  │     去重 → 契约 → 输出截断 → 安全 → 审计 → 死信
  │
  ├── 结果汇总到 SpawnAccumulator
  │
  └── 返回 ChildResult 给父 Agent
```

### peer（水平接力）

```
handoff_to_peer(peer="writer", task="写文章")
  │
  ├── run.park_handoff()——父运行标记为 COMPLETED + pending_handoff
  │
  ├── relay——接力执行
  │     fork_as_peer()——派生 peer Agent
  │     peer 运行（继承父的 wiring，保持自己的 peers）
  │     peer 完成后 → 结果返回给调用方
  │
  └── 父运行不再继续（因为已经 COMPLETED）
```

### 统一消息平面

无论是 spawn 还是 peer，跨 Agent 边界的消息都经过 `Crossing` 消息平面：

```
Crossing（消息信封）
  ├── id：唯一标识（去重用）
  ├── from_agent / to_agent
  ├── direction：DOWNSTREAM / UPSTREAM
  ├── payload：消息内容
  └── meta：元数据（turns, cost, depth, parent_run_id...）

管道（pipeline）：
  去重 → 契约校验 → 安全拦截 → 审计记录 → 死信队列
```

---

## 阶段 10：终止

**源码：** `src/prodagent/kernel/loop.py` — `stream()` 的 finally 块

### 三种终止状态

```python
# COMPLETED——正常完成
run.state = RunState.COMPLETED
run.final_output = "..."

# FAILED——预算耗尽 / 死循环 / 异常
run.state = RunState.FAILED
run.last_error = "BudgetExceeded: ..."
run.error = classify_error(exc, layer=ErrorLayer.RUNTIME)

# SUSPENDED——等待 HITL 审批
run.state = RunState.SUSPENDED
run.pending_tool_call = ToolCall(...)
run.pending_approval_id = "req_123"
```

### 终止时的清理

```python
finally:
    if self._checkpoint_store is not None:
        await save_and_fire_checkpoint(self._checkpoint_store, run, self._hooks)
```

无论正常完成还是异常终止，finally 块都会保存最后一次 checkpoint。这确保了：

- COMPLETED 的 run 有最终状态
- FAILED 的 run 有错误信息
- SUSPENDED 的 run 有恢复点

### span 结束

```python
await self._end_run_span(run, error=str(exc) if exc else None)
# fire(LOOP_END, run_id=..., error=...)
```

---

## 阶段 11：会话保存

**源码：** `src/prodagent/runtime/agent.py` — `chat_stream()` 中的事件处理

### 做什么

```python
async for event in drive_stream(...):
    if isinstance(event, (RunCompletedEvent, RunFailedEvent, RunSuspendedEvent)):
        session.complete_turn(run_id, resolved_mode, event.run)
        await store.save(session, expected_version=session.version)
    yield event
```

当收到终端事件时：

1. `session.complete_turn()`——把本次 run 的结果记录到会话历史
2. `store.save(session, expected_version=session.version)`——乐观并发保存会话

### 为什么在 chat_stream 里做，而不是在 loop 里做

因为会话是多轮的概念，loop 是单轮的概念。loop 不知道"这是会话的第几轮"，也不知道"会话历史"。会话管理在 `Agent.chat_stream()` 层面做，loop 只负责单轮运行。

---

## 阶段 12：返回

**源码：** `src/prodagent/runtime/runner.py` — `collect_final_run()`

### 做什么

```python
async def collect_final_run(stream, *, fallback_run_id, fallback_task):
    final_run = None
    async for event in stream:
        if isinstance(event, (RunCompletedEvent, RunFailedEvent, RunSuspendedEvent)):
            final_run = event.run
    if final_run is None:
        # 流结束了但没有终端事件 → 构造一个 FAILED run
        final_run = make_failed_run(fallback_run_id, fallback_task)
    return final_run
```

### 为什么需要 fallback

如果流在没有产生终端事件的情况下结束了（比如消费者中途取消了流、或者某个未处理的异常导致流提前结束），`final_run` 会是 None。这种情况下构造一个 `FAILED` run，确保 `chat()` 永远返回一个有效的 `AgentRun`。

---

## 总结：数据在各阶段的形态变化

跟踪 `AgentRun` 在一次调用中的形态变化：

```
阶段 1（入口）：
  ConversationSession 被创建/加载

阶段 2（装配）：
  RunContext 被构建（llm, dispatcher, hooks, budget, ...）

阶段 4（循环初始化）：
  AgentRun 被创建/恢复
  run.state = RUNNING
  run.messages = [user_message]
  run.metrics = 全零

阶段 5（think）：
  run.metrics.turn_count = 1
  run.metrics.input_tokens = 1500
  run.metrics.output_tokens = 200
  run.metrics.cost_usd = 0.002
  run.messages = [user, assistant(content="", tool_calls=[...])]

阶段 7（execute）：
  run.messages = [user, assistant(...), tool(result="...")]
  run.tool_history = [ToolCall(name="search", ...)]

阶段 8（结算）：
  run.last_event_seq = 1
  checkpoint 被保存

阶段 5（第二轮 think）：
  run.metrics.turn_count = 2
  run.messages = [..., assistant(content="答案", stop_reason=END_TURN)]

阶段 10（终止）：
  run.state = COMPLETED
  run.final_output = "答案"
  最后一次 checkpoint 被保存

阶段 11（会话保存）：
  ConversationSession.turns 追加一条记录
  session.version += 1

阶段 12（返回）：
  返回 AgentRun（state=COMPLETED, final_output="答案"）
```

---

## 关键洞察

1. **AgentRun 是唯一的可变状态**——整个运行过程中，所有状态变化都反映在 `AgentRun` 上。checkpoint 就是序列化 `AgentRun`，恢复就是反序列化 `AgentRun`。

2. **Step 是原子的**——一次 `think → decide → execute` 要么完整执行，要么不执行。checkpoint 在 Step 之间保存，恢复时从下一个 Step 开始。

3. **预算检查无处不在**——思考前、调用中、工具后，每个关键节点都检查。这是"硬约束"的体现。

4. **横切关注点通过总线接入**——审批、可观测、记忆、审计，都通过 `HookRegistry` 的 fire/check/collect 接入。循环本身不知道它们的存在。

5. **错误是反馈而非崩溃**——工具错误返回 `ToolResult`，模型看到错误后自己修正。只有不可恢复的错误（预算耗尽、死循环）才会终止运行。

---

## 下一步

- 想理解架构的整体设计？→ [架构全景](architecture.md)
- 想理解每个设计决策的"为什么"？→ [设计哲学](design-philosophy.md)
- 想跟着七站之旅逐步学习？→ [第一部分 · 一次调用的生命周期](tour/index.md)
- 想自己动手跑一遍？→ [5 分钟上手](start.md)
