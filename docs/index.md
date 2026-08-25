# prodagent — 一份你真的能读完的工业级 Agent 实现

> **25,000 行 · 14 个包 · 1,182 个离线测试 · 核心仅 4 个依赖**
>
> 循环、预算、恢复、审批、权限、可观测、评估、多 Agent 协作——每个机制都小到一次读懂，完整到能上生产。

---

## 这不是又一个 Agent 框架

市面上的 Agent 项目走两个极端：

| | 黑盒框架（LangChain / AutoGen） | 教学玩具（几十行 ReAct 演示） | **prodagent** |
|---|---|---|---|
| 功能完整度 | 高 | 低 | **高** |
| 代码可读性 | 抽象层厚，改不动核心 | 清晰但缺生产机制 | **25,000 行，按学习顺序排列** |
| 预算 / 恢复 / 审批 | 无或绑死云上 | 无 | **内建，默认开启** |
| 企业级特性 | 绑死它们的云 | 无 | **权限 / 可观测 / 评估，可拆解搬运** |
| 你学会的是 | 它的 API | ReAct 概念 | **一套能上生产的 Agent 设计心智模型** |

**prodagent 卡在中间。** 它是一个可以上生产的库，同时整个代码库小到你可以从头读到尾，在脑子里建立完整的心智模型。

---

## 你会得到什么

### 一套完整的生产级 Agent 设计心智模型

不是零散的知识点，而是从一次 `chat()` 调用开始，经过 **循环 → 预算 → 工具调度 → 审批 → 权限校验 → 检查点 → 消息平面 → 上下文压缩 → 记忆召回 → 可观测追踪**，到最终返回的完整生命周期。

读完你能在白板上画出整个 Agent 的运行时架构，并且说清每一层**为什么存在、去掉会怎样、替代方案是什么**。

### 每个机制都能现场调试

整个框架离线可跑。测试不连网，9 个示例的每一轮模型行为都是可复现的脚本。

你可以：
- 在 `chat()` 的调用链打断点，看一次请求到底经过了多少层
- 改一个预算参数，看它在哪一轮、为什么停下来
- 把审批门拆掉，看没有保护的 Agent 会做出什么
- 把权限策略改成全放行，看越权操作会触发什么

> **理解一个机制最深刻的方式是调试它，不是背它的结论。**

---

## 学习路线

```mermaid
graph LR
    A[🚀 5分钟上手] --> B[第一部分<br/>一次调用的生命周期]
    B --> C[第二部分<br/>生产问题域深度]
    C --> D[实战示例地图]
    D --> E[附录<br/>取舍与术语]
```

### 🚀 第一步：[5 分钟上手](start.md)

零文件零旁路，跑通最小 Agent。

### 📖 第一部分：一次调用的生命周期

用七站走完一条完整链路，每站对应源码里的一个包：

| 站 | 主题 | 解决什么问题 | 源码包 |
|----|------|-------------|--------|
| ① | [核心词汇](tour/01-core.md) | Agent、Run、Step、Turn、Message 到底是什么关系 | `kernel/types` |
| ② | [端口与契约](tour/02-ports.md) | 为什么用 Protocol 而不是继承？14 个端口怎么分工 | `ports/` |
| ③ | [模型层](tour/03-llm.md) | LLMClient 端口、流式回调、缓存边界、定价模型 | `llm/` |
| ④ | [工具系统](tour/04-tools.md) | `@tool` 装饰器、参数校验、只读并行/写串行、工具幻觉防御 | `tooling/` |
| ⑤ | [循环内核](tour/05-loop.md) | think→decide→execute 原子、死循环检测、终止与恢复 | `kernel/` |
| ⑥ | [规划与 DAG](tour/06-plan.md) | REACTIVE vs PLAN_FIRST vs Workflow，动态 DAG 断点续跑 | `plan/` `runtime/` |
| ⑦ | [多 Agent 协作](tour/07-multiagent.md) | 委派/接力/投票/黑板/队列五种拓扑，统一消息平面 | `coordination/` |

### 🔧 第二部分：生产问题域

| 领域 | 主题 | 核心机制 |
|------|------|---------|
| 运行时稳定性 | [崩溃恢复](topics/recovery.md) | checkpoint + 乐观版本控制，kill -9 后续跑不重复 |
| | [四轴预算](topics/budget.md) | turns/seconds/tokens/cost 任一触顶即停，子 Agent 实时汇总 |
| | [HITL 审批](topics/approval.md) | HIGH 副作用工具挂起，拒绝触发增量重规划 |
| 上下文与记忆 | [五级压缩](topics/compression.md) | 按 token 占比分级牺牲，关键约束不被压缩掉 |
| | [四通道记忆](topics/memory.md) | 规则/实体/精确/语义并行召回 + 冲突裁决 |
| | [技能闭环](topics/skills.md) | 成功 run 蒸馏成 runbook，越用越稳 |
| 多 Agent 治理 | [策略与治理](topics/governance.md) | 死循环兜底、消息不丢不重不乱序、权限策略引擎 |
| 可观测与评估 | [全链路追踪](topics/observability.md) | OpenTelemetry 兼容 span，CoT 思维链落盘 |
| | [评估与回归](topics/evaluation.md) | 离线评测 + 线上 Trace 自动打分，LLM-as-judge 校准 |

### 🎯 实战：[9 个端到端示例](examples.md)

每个示例对应一个真实生产场景，教一个完整的机制组合。

### 📚 附录

- [设计取舍](decisions.md) — 每个关键决策的"为什么不选另一种"
- [术语表](glossary.md) — 快速查阅
- [API 参考](reference.md) — 自动生成的接口文档

---

## 核心能力速览

| 能力 | 一句话 | 关键源码 |
|------|--------|---------|
| **裸核默认** | `Agent()` 零文件起步；`production()` 一键全套护甲 | `core/config.py` |
| **四轴硬预算** | 任一轴触顶即停，子 Agent 花销实时汇总到总账 | `kernel/budget.py` |
| **崩溃恢复** | checkpoint + 乐观并发，断点续跑不重复执行 | `ports/checkpoint.py` |
| **HITL 审批** | HIGH 工具挂起等人，拒绝→增量重规划 | `hooks/approval/` |
| **权限策略** | RBAC + 操作级授权，三层策略越权拦截 | `hooks/authorization/` |
| **三执行模式** | REACTIVE / PLAN_FIRST / Workflow 按复杂度选 | `runtime/` `plan/` |
| **五协作原语** | 委派/接力/投票/黑板/队列覆盖主流拓扑 | `coordination/` |
| **统一消息平面** | 去重→契约→安全→审计→死信，不丢不重不乱序 | `coordination/messaging/` |
| **五级上下文压缩** | 按 token 占比分级牺牲，语义损失有明确边界 | `cognition/context/` |
| **四通道记忆** | 规则/实体/精确/语义并行召回 + 冲突裁决 | `cognition/memory/` |
| **全链路可观测** | span 追踪 + CoT 落盘 + Trace/Log/Metrics 联动 | `hooks/observability/` |
| **评估回归** | 离线数据集 + 线上 Trace 打分，改动可跑回归对比 | `evaluation/` |
| **可替换后端** | 14 个 Protocol 端口，file+memory 零依赖起步 | `ports/` `backends/` |
| **MCP 桥接** | 外部工具经 stdio/HTTP 接入，不用逐个写适配 | `mcp/` |

---

## 包目录即学习顺序

```
src/prodagent/
├── core/          ← 基础工具：配置、异常、重试、事件日志
├── ports/         ← 14 个 Protocol 端口（六边形架构的"左"侧）
├── llm/           ← 模型适配器：OpenAI/Anthropic/Fake + 定价
├── tooling/       ← 工具系统：装饰器、调度、注册、可靠性
├── kernel/        ← 内核：循环、步骤、预算、事件总线、状态
├── runtime/       ← 运行时：Agent 装配、工厂、父运行时
├── plan/          ← 规划：动态 DAG、PlanExecutor、Workflow
├── coordination/  ← 多 Agent：spawn/peer/ensemble/board/queue
├── cognition/     ← 认知：上下文压缩、四通道记忆
├── hooks/         ← 横切：审批、权限、可观测、审计
├── skills/        ← 技能：runbook 蒸馏与召回
├── evaluation/    ← 评估：数据集、打分、回归对比
├── backends/      ← 端口实现：file/memory/postgres/redis/neo4j
├── mcp/           ← MCP 桥接：stdio/HTTP 外部工具
└── playground/    ← 可视化（叶子节点，被 import-linter 隔离）
```

---

## 安装

```bash
pip install prodagent
# 核心仅 4 个依赖：anyio / httpx / pydantic / typing-extensions

# 按需加装：
pip install "prodagent[openai,anthropic]"   # 模型 provider
pip install "prodagent[playground]"          # 可视化
pip install "prodagent[postgres,redis,neo4j]" # 生产后端
```

模型配置三选一：
- `USE_FAKE_LLM=1` — 完全离线，学习/测试用
- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` — 任意 OpenAI 兼容端点
- `ANTHROPIC_API_KEY` — Anthropic 原生

---

## 下一步

👉 **[从 5 分钟上手开始 →](start.md)**

或者直接跳进 [第一部分 · 一次调用的生命周期](tour/index.md)。
