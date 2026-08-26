# 第 ⑥ 站：规划与 DAG

> 不是所有任务都适合"边走边想"。这一站讲清楚两种执行模式怎么选、PLAN_FIRST 的动态 DAG 怎么做断点续跑、Workflow 怎么手写确定性计划。

---

## 问题：一种循环走天下吗？

```mermaid
graph TD
    Q{"任务类型?"} -->|探索式、不确定路径| R["REACTIVE<br/>边走边想"]
    Q -->|有明确步骤、可并行| P["PLAN_FIRST<br/>先规划后执行"]
    Q -->|确定性流程、合规要求固定| W["Workflow<br/>手写静态 DAG"]
    style R fill:#e8f5e9,stroke:#2e7d32
    style P fill:#fff3e0,stroke:#e65100
    style W fill:#e3f2fd,stroke:#1565c0
```

REACTIVE 模式很灵活，但不是万能的：
- 任务有 5 个独立子任务可以并行，REACTIVE 只能串行做
- 合规审计要求按固定步骤执行，REACTIVE 可能跳过或乱序
- 长任务跑到一半中断，REACTIVE 恢复后可能重复执行已完成的步骤

prodagent 提供两种执行模式 + 一个 Workflow 构建器，按任务复杂度选择。

---

## 两种执行模式 + Workflow

| 维度 | REACTIVE | PLAN_FIRST | Workflow |
|------|----------|------------|----------|
| 规划方式 | 无，边走边想 | 模型先输出 DAG | 代码手写 DAG |
| 模型参与 | 每轮都参与 | 规划时参与，执行时可选 | 仅 llm_step 中调用 |
| 并行能力 | 单轮内只读工具并行 | DAG 节点可并行 | DAG 节点可并行 |
| 断点续跑 | checkpoint 恢复整轮 | 按节点状态恢复 | 按节点状态恢复 |
| 确定性 | 低（每轮模型决策） | 中（计划固定，执行可调） | 高（完全固定） |
| 适合场景 | 探索、研究、调试 | 复杂任务、多步骤 | 合规、流水线 |
| 类比 | 开车去陌生地方 | 装修先出施工图 | 工厂流水线 |

> **注意**：`ExecutionMode` 枚举只有 `REACTIVE` 和 `PLAN_FIRST` 两个值。Workflow 不是第三种执行模式，而是一个独立的计划构建器——你用代码手写 DAG，通过 `workflow=` 参数传给 Agent，Agent 在 PLAN_FIRST 模式下执行这个预编译的 Plan。

---

## REACTIVE：边走边想

```python
agent = Agent("demo", mode=ExecutionMode.REACTIVE)
```

**执行流程**：

```
while True:
    response = llm.complete(messages)
    if response.stop_reason == StopReason.END_TURN:
        return response.content
    execute_tools(response.tool_calls)
```

每一轮模型自己决定"下一步做什么"。没有预先计划，完全根据当前上下文决策。

**优点**：灵活，能应对意外情况，适合探索式任务
**缺点**：可能绕路、可能重复、长任务容易失控（靠预算兜底）

详细的循环机制见 [第 ⑤ 站：循环内核 →](05-loop.md)。

---

## PLAN_FIRST：先规划后执行

```python
agent = Agent("demo", mode=ExecutionMode.PLAN_FIRST)
```

### 执行流程

```mermaid
graph TD
    START["开始"] --> PLAN["① 规划阶段<br/>模型输出 DAG 计划"]
    PLAN --> VALIDATE["② 校验 DAG<br/>依赖关系、节点合法性"]
    VALIDATE --> EXEC["③ 执行阶段<br/>按依赖关系调度节点"]
    EXEC --> NODE{"节点状态?"}
    NODE -->|需要模型决策| LLM["调用模型执行该步骤"]
    NODE -->|纯工具调用| TOOL["直接执行工具"]
    NODE -->|失败| REPLAN{"④ 增量重规划?"}
    REPLAN -->|是| ADJUST["调整后续 DAG<br/>不推倒重来"]
    REPLAN -->|否| FAIL["标记失败"]
    NODE -->|完成| CHECK{"还有未完成节点?"}
    CHECK -->|有| EXEC
    CHECK -->|无| DONE["完成"]
```

### ① 规划阶段

模型输出一个 JSON 格式的 DAG：

```json
{
  "steps": [
    {"id": "search", "action": "search", "params": {"query": "..."}, "depends_on": []},
    {"id": "analyze", "action": "analyze", "params": {}, "depends_on": ["search"]},
    {"id": "report", "action": "write_report", "params": {}, "depends_on": ["analyze"]}
  ]
}
```

每个步骤有：
- `id` — 唯一标识
- `action` — 执行什么（工具名或模型决策）
- `params` — 参数
- `depends_on` — 依赖哪些步骤

### ② 校验 DAG

- 检查是否有循环依赖（DAG 必须无环）
- 检查依赖的节点是否存在
- 检查 action 是否是已注册的工具
- 校验失败返回错误，让模型重新规划

### ③ 执行阶段

PlanExecutor 按依赖关系调度：
- 没有依赖的节点可以**并行**执行
- 有依赖的节点等前置节点完成后执行
- 每个节点执行后更新状态（PENDING → RUNNING → COMPLETED/FAILED）

### ④ 增量重规划

这是 PLAN_FIRST 最有价值的特性。当某个节点失败（比如审批被拒、工具报错），不是推倒整个计划重来，而是：
1. 标记该节点为 FAILED
2. 把失败原因告诉模型
3. 模型**只调整受影响的后续节点**，已完成的节点不动
4. 继续执行调整后的 DAG

```
原计划: A → B → C → D
执行中: A ✅ → B ❌(审批被拒)
重规划: A ✅ → B' → C' → D   (A不动，B/C/D调整)
```

**为什么不推倒重来？**
- 已完成的步骤可能有副作用（发了邮件、写了数据库），重复执行危险
- 浪费 token 和时间
- 增量重规划更高效、更安全

---

## Workflow：手写确定性 DAG

Workflow 是一个**计划构建器**，让你用代码（而不是让模型）定义 DAG：

```python
from prodagent.plan import Workflow

wf = Workflow()

# ① 普通函数步骤——用 @wf.step 装饰器注册
@wf.step
async def fetch_data() -> str:
    """获取数据"""
    return "raw data"

@wf.step(depends_on=["fetch_data"])
async def analyze(fetch_data: str) -> str:
    """分析数据——参数名与依赖名匹配时自动绑定 {{fetch_data.output}}"""
    return f"analyzed: {fetch_data}"

# ② LLM 步骤——调用模型处理
wf.llm_step(
    name="summarize",
    prompt="总结分析结果：{{analyze.output}}",
    depends_on=["analyze"],
    is_terminal=True,
)

# ③ 工具步骤——调用已注册的工具
wf.tool_step(
    name="save_report",
    tool_name="write_file",
    params={"path": "report.md", "content": "{{summarize.output}}"},
    depends_on=["summarize"],
)

# 编译为 Plan
plan = wf.compile()

# 绑定到 Agent——Agent 在 PLAN_FIRST 模式下执行这个预编译的 Plan
agent = Agent(
    "demo",
    tools=[...],
    workflow=wf,  # Agent 构造时自动 bind(llm, hooks) 并 compile
    config=AgentConfig(name="demo"),
)
```

### Workflow API

| 方法 | 作用 |
|------|------|
| `wf.step(fn, *, name, depends_on, is_terminal, params)` | 注册普通函数步骤（可作装饰器） |
| `wf.llm_step(name, prompt, *, system, depends_on, is_terminal, params, config, timeout_ms)` | 注册一个调用 LLM 的步骤 |
| `wf.tool_step(name, tool_name, *, params, depends_on, is_terminal)` | 注册一个调用已有工具的步骤 |
| `wf.compile() -> Plan` | 编译为可执行的 Plan |
| `wf.bind(llm, hooks)` | 绑定 LLM 客户端和事件总线（Agent 构造时自动调用） |
| `wf.tools` | 返回所有步骤生成的 FunctionTool 列表 |

**自动参数绑定**：`@wf.step` 装饰的函数，如果参数名与依赖名匹配，自动绑定为 `{{dep.output}}` 模板引用。

**Agent 步骤**：`wf.step` 也可以直接接收一个 Agent 实例作为子步骤，会生成 `spawn_agent` 动作。

### 与 PLAN_FIRST 的区别

- DAG 是代码预定义的，模型不参与规划
- 每个步骤是普通函数或 LLM 调用，不是模型自由决策
- 完全确定，适合合规要求固定路径的场景
- `llm_step` 中模型只负责处理该步骤的输入，不决定流程

**适合场景**：
- 合规审计（必须按法规规定的步骤执行）
- 数据流水线（ETL）
- 审批流程（固定的审批节点）

---

## DAG 的断点续跑

PLAN_FIRST 和 Workflow 都支持 DAG 级别的断点续跑。

```mermaid
graph LR
    A["步骤1<br/>✅ COMPLETED"] --> B["步骤2<br/>✅ COMPLETED"]
    B --> C["步骤3<br/>💥 执行中被杀"]
    B --> D["步骤4<br/>⏳ PENDING"]
    C --> E["步骤5<br/>⏳ PENDING"]
    D --> E
    style A fill:#c8e6c9,stroke:#2e7d32
    style B fill:#c8e6c9,stroke:#2e7d32
    style C fill:#ffebee,stroke:#c62828
    style D fill:#fff3e0,stroke:#e65100
    style E fill:#fff3e0,stroke:#e65100
```

恢复时：
1. 加载 DAG 状态（每个节点的 PENDING/RUNNING/COMPLETED/FAILED/OBSOLETE/SUSPENDED）
2. **COMPLETED 节点不重复执行**
3. RUNNING 节点重置为 PENDING（不知道执行到哪了，安全起见重做）
4. 按依赖关系继续执行

DAG 状态存在 AgentRun 里，和消息历史一起序列化到 checkpoint。Plan 的状态变更还可以通过 EventLog 追加写入，支持事件溯源重建。

---

## 模式选择决策树

```mermaid
graph TD
    START["选择策略"] --> Q1{"任务路径<br/>确定吗？"}
    Q1 -->|完全确定| Q2{"合规要求<br/>固定路径吗？"}
    Q2 -->|是| WORKFLOW["Workflow<br/>手写静态 DAG"]
    Q2 -->|否| Q3{"有可并行的<br/>独立子任务吗？"}
    Q3 -->|有| PLAN["PLAN_FIRST"]
    Q3 -->|没有| REACTIVE["REACTIVE"]
    Q1 -->|不确定| Q4{"任务复杂吗<br/>(>5步)?"}
    Q4 -->|复杂| PLAN
    Q4 -->|简单| REACTIVE
    style WORKFLOW fill:#e3f2fd,stroke:#1565c0
    style PLAN fill:#fff3e0,stroke:#e65100
    style REACTIVE fill:#e8f5e9,stroke:#2e7d32
```

**经验法则**：
- 不确定、探索性的 → REACTIVE
- 复杂、多步骤、可并行 → PLAN_FIRST
- 固定流程、合规要求 → Workflow（手写 DAG 传给 PLAN_FIRST）

---

## 模式间的共享内核

两种模式看起来不同，但底层共享同一个 `Step` 原子：

```
REACTIVE:   while True: Step.run()
PLAN_FIRST: for node in ready_nodes: Step.run()  (按 DAG 调度)
Workflow:   for node in ready_nodes: function/llm/tool  (编译为 Plan 后由 PLAN_FIRST 执行)
```

`Step` 的"想→做→记账"逻辑是共用的，经过了框架 1,000+ 个测试的验证。两种模式的差异只在"什么时候调用 Step"和"调用哪个 Step"。

> 这就是为什么把 kernel 从 runtime 拆出来——核心循环逻辑只写一次、测一次，两种模式共享。

---

## 代码定位

| 内容 | 源码位置 |
|------|---------|
| ExecutionMode 枚举 | `base/types.py` |
| ReactiveLoop | `kernel/loop.py` |
| Plan / PlanStep DAG 结构 | `plan/dag.py` |
| Planner（模型输出 DAG） | `plan/planner.py` |
| PlanExecutor（DAG 调度） | `plan/executor.py` |
| StepRunner（单步执行） | `plan/step_runner.py` |
| Workflow 构建器 | `plan/workflow.py` |
| Plan 事件溯源 | `plan/event_log.py` |
| Agent 装配与模式选择 | `runtime/agent.py` `runtime/factory.py` |

---

## 下一步

👉 **[第 ⑦ 站：多 Agent 协作 →](07-multiagent.md)** — 五种协作原语怎么选？统一消息平面怎么工作？

或者深入 [HITL 审批专题 →](../topics/approval.md)，看审批拒绝如何触发增量重规划。
