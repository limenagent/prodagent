# prodagent

> **生产级 LLM Agent 框架——小到能读完，完整到能上生产，美到想收藏。**
>
> 25,000 行 · 14 个包 · 1,182 个离线测试 · 核心仅 4 个依赖

[![PyPI](https://img.shields.io/pypi/v/prodagent?color=blue&logo=pypi&logoColor=white)](https://pypi.org/project/prodagent/)
[![Python](https://img.shields.io/pypi/pyversions/prodagent?logo=python&logoColor=white)](https://pypi.org/project/prodagent/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/limenagent/prodagent/ci.yml?logo=github&label=CI)](https://github.com/limenagent/prodagent/actions)
[![Tests](https://img.shields.io/badge/tests-1%2C182-offline-green)]()
[![Dependencies](https://img.shields.io/badge/core%20deps-4-purple)]()

**中文** · [English](README.en.md) · 极客时间专栏[《生产级 Agent 排雷实战》](http://gk.link/a/12L6Q)配套框架

---

## 一句话

**prodagent 是一个你能从头读到尾、在脑子里建立完整心智模型的工业级 Agent 框架。**

它不是又一个黑盒 SDK，也不是几十行的教学玩具。它卡在中间：每一个机制——循环、预算、恢复、审批、权限、可观测、多 Agent 协作——都小到一次读懂，完整到能直接搬上生产。

---

## 你会用 LangChain，但你能设计一个 Agent 系统吗？

翻一翻 2026 年的 Agent 岗位，分层很清楚。

- **初级岗**："熟练使用 LangChain/LangGraph，会写 prompt，做过 RAG"——三个月上手，供给最卷。
- **高级岗 / 架构师岗**："从 0 到 1 构建生产级 Agent 系统"、"深度定制或自研核心模块"、"多智能体架构设计"——薪资翻两到三倍，能接住的人很少。

面试没人问你"LangChain 的 AgentExecutor 怎么初始化"，他们问的是：

- 一个 `while True` 调模型的循环，上生产之前要加多少层护甲？
- turns / seconds / tokens / cost 四轴预算怎么同时生效，任一触顶即停？
- 长任务跑到一半进程被 `kill -9`，怎么从断点续跑，不丢状态也不重复执行？
- 模型调用了不存在的工具、传错了参数，怎么防？
- 什么时候该拆多 Agent，什么时候单 Agent 加好上下文管理就够了？
- 改了一版 prompt，怎么知道变好还是变差？

这些问题，论文给不了答案，API 文档也给不了。解法散落在 issue 讨论、云厂商文档、某个开源框架的源码里——你得自己翻几十万行、自己拼。

**这个仓库已经帮你串好了。**

---

## 为什么是 prodagent，而不是别的

市面上的 Agent 项目走两个极端，prodagent 卡在中间：

| | 黑盒框架（LangChain / AutoGen） | 教学玩具（几十行 ReAct） | **prodagent** |
|---|---|---|---|
| 功能完整度 | 高 | 低 | **高** |
| 代码可读性 | 抽象层厚，改不动核心 | 清晰但缺生产机制 | **25,000 行，按学习顺序排列** |
| 预算 / 恢复 / 审批 | 无或绑死云上 | 无 | **内建，默认开启** |
| 核心依赖 | 数十个（间接） | 1-2 个 | **4 个** |
| 测试 | 依赖真实 API | 几乎没有 | **1,182 个，全离线可复现** |
| 企业级特性 | 绑死它们的云 | 无 | **权限 / 可观测 / 评估，可拆解搬运** |
| 你学会的是 | 它的 API | ReAct 概念 | **Agent 系统的设计心智模型** |

**核心理念：每个机制都是独立模块，有清晰的 Protocol 边界。你的项目缺哪块，就搬哪块，不用引入整个框架。**

---

## 30 秒看它能做什么

### 最小可跑，零文件零旁路

```python
import asyncio
from prodagent import Agent, ExecutionMode, tool

@tool(name="search", readonly=True)
async def search(query: str) -> str:
    return f"results for: {query}"

agent = Agent("demo", system_prompt="Find answers.",
              tools=[search], mode=ExecutionMode.REACTIVE)

asyncio.run(agent.chat("巴黎今天天气如何？"))
```

### 一键上生产全套护甲

落盘恢复 + span 追踪 + HIGH 工具审批 + 权限策略 + LLM 缓存 + 上下文压缩，一行切换：

```python
from prodagent import Agent, AgentConfig
from prodagent.base.config import production

agent = Agent("demo", tools=[search],
              config=AgentConfig(name="demo", framework=production()))
```

### 一次 `chat()` 调用内部经过的完整链路

```
Agent.chat()
  → RunLoop（运行时入口）
    → ReactiveLoop / PlanExecutor / Workflow（三种执行模式）
      → Step（think → decide → execute 原子）
        → 预算检查（turns/seconds/tokens/cost 四轴）
        → 死循环检测（fingerprint 窗口）
        → 上下文组装（记忆召回 + 五级压缩）
        → LLM 调用（硬超时 + 流式 + 缓存边界）
        → 工具调度（只读并行 / 写串行）
          → 权限校验（RBAC + 操作级授权）
          → HITL 审批门（HIGH 工具挂起等人）
          → 工具执行 + 结果写回
        → checkpoint 落盘（乐观版本控制）
      → 多 Agent 协作（spawn/peer/ensemble/board/queue）
        → Crossing 消息平面（去重→契约→安全→审计→死信）
  → 返回结果
```

---

## 关键数字

| 指标 | 值 | 说明 |
|------|-----|------|
| 代码行数 | **25,000** | 整个 codebase，小到可以从头读到尾 |
| 包数量 | **14** | 按学习顺序排列，每个包一个职责 |
| 离线测试 | **1,182** | 零 API key、零网络、零 flaky，FakeLLM 精确复现 |
| 核心依赖 | **4** | anyio / httpx / pydantic / typing-extensions |
| 协议端口 | **14** | Protocol 抽象，后端可替换（file/memory/postgres/redis/neo4j） |
| 执行模式 | **3** | REACTIVE / PLAN_FIRST / Workflow |
| 协作原语 | **5** | 委派 / 接力 / 投票 / 黑板 / 工作队列 |
| 总线协议 | **3** | fire（观察）/ check（拦截）/ collect（注入） |
| 预算轴 | **4** | turns / seconds / tokens / cost |
| 压缩级 | **5** | 无压缩 → 工具压缩 → 历史摘要 → 主题摘要 → 紧急截断 |
| 记忆通道 | **4** | 规则 / 实体 / 精确 / 语义 |
| Python 版本 | 3.11 - 3.14 | CI 矩阵全覆盖 |

---

## 设计思想精华（10 条）

> 完整论述见 [设计哲学](docs/design-philosophy.md)。

1. **裸核默认，生产一键**——`Agent()` 零文件起步；`production()` 一行恢复全套护甲。不是"默认全关让你自己开"，而是"默认够用，升级一键"。
2. **纯内核**——`kernel/` 不依赖任何 capability 包。循环、预算、状态、总线都是纯逻辑，可以独立测试、独立替换。
3. **三协议总线**——一个接缝连接所有横切关注点：`fire`（观察，并发扇出）、`check`（拦截，串行否决）、`collect`（注入，并发收集）。循环本身永远不知道审批、可观测、记忆的名字。
4. **四轴预算互为兜底**——turns / seconds / tokens / cost 任一轴都可能被绕过，但四轴同时生效就很难失控。子 Agent 花销通过共享 `BudgetLedger` 实时汇总。
5. **结构化错误而非异常**——模型传错参数是正常现象，不是异常。返回 `ToolError` + 修正建议，模型下一轮自己修正。抛异常会打断循环，用户看到的是"程序崩了"。
6. **五级压缩有明确边界**——按 token 占比分级牺牲：无压缩 → 工具结果压缩 → 历史摘要 → 主题摘要 → 紧急截断。L0 系统提示和 L1 状态块永远不被压缩。
7. **单 Agent 是默认**——先把单 Agent 的上下文管理做好（记忆、压缩、技能），实在搞不定再拆多 Agent。很多"需要多 Agent"的场景，其实是单 Agent 的上下文管理没做好。
8. **所有拓扑共用一个消息平面**——五种协作原语的通信都走 `Crossing` 管道：去重 → 契约 → 安全 → 审计 → 死信。在一个地方解决"不丢不重不乱序"，比在五个拓扑里各写一遍更可靠。
9. **乐观并发恢复**——checkpoint + 版本控制，`kill -9` 后续跑不重复执行。不需要分布式锁，因为一个 Run 通常只有一个执行者，冲突概率极低。
10. **全离线可复现**——1,182 个测试零 API key、零网络。FakeLLM 精确控制每一轮输出，测试确定性场景。理解一个机制最深刻的方式是调试它，不是背它的结论。

---

## 安装

```bash
pip install prodagent

# 核心仅 4 个依赖，按需加装：
pip install "prodagent[openai,anthropic]"   # 模型 provider
pip install "prodagent[playground]"          # 可视化 playground
pip install "prodagent[postgres,redis,neo4j]" # 生产后端
```

模型配置三选一：

- `USE_FAKE_LLM=1` — 完全离线，学习/测试用
- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` — 任意 OpenAI 兼容端点（DeepSeek/Qwen/Moonshot…）
- `ANTHROPIC_API_KEY` — Anthropic 原生

---

## 学习路线

```
5 分钟上手
  → 第一部分：一次调用的生命周期（七站）
    → 第二部分：生产问题域深度专题
      → 实战示例地图
        → 附录：取舍与术语
```

### 🚀 第一步：[5 分钟上手](docs/start.md)

零文件零旁路，跑通最小 Agent。

### 📖 第一部分：一次调用的生命周期

用七站走完一条完整链路，每站对应源码里的一个包：

| 站 | 主题 | 解决什么问题 | 源码包 |
|----|------|-------------|--------|
| ① | [核心词汇](docs/tour/01-core.md) | Agent、Run、Step、Turn、Message 到底是什么关系 | `kernel/types` |
| ② | [端口与契约](docs/tour/02-ports.md) | 为什么用 Protocol 而不是继承？14 个端口怎么分工 | `ports/` |
| ③ | [模型层](docs/tour/03-llm.md) | LLMClient 端口、流式回调、缓存边界、定价模型 | `llm/` |
| ④ | [工具系统](docs/tour/04-tools.md) | `@tool` 装饰器、参数校验、只读并行/写串行、工具幻觉防御 | `tooling/` |
| ⑤ | [循环内核](docs/tour/05-loop.md) | think→decide→execute 原子、死循环检测、终止与恢复 | `kernel/` |
| ⑥ | [规划与 DAG](docs/tour/06-plan.md) | REACTIVE vs PLAN_FIRST vs Workflow，动态 DAG 断点续跑 | `plan/` `runtime/` |
| ⑦ | [多 Agent 协作](docs/tour/07-multiagent.md) | 委派/接力/投票/黑板/队列五种拓扑，统一消息平面 | `coordination/` |

### 🔧 第二部分：生产问题域

| 领域 | 主题 | 核心机制 |
|------|------|---------|
| 运行时稳定性 | [崩溃恢复](docs/topics/recovery.md) | checkpoint + 乐观版本控制，kill -9 后续跑不重复 |
| | [四轴预算](docs/topics/budget.md) | turns/seconds/tokens/cost 任一触顶即停，子 Agent 实时汇总 |
| | [HITL 审批](docs/topics/approval.md) | HIGH 副作用工具挂起，拒绝→增量重规划 |
| 上下文与记忆 | [五级压缩](docs/topics/compression.md) | 按 token 占比分级牺牲，关键约束不被压缩掉 |
| | [四通道记忆](docs/topics/memory.md) | 规则/实体/精确/语义并行召回 + 冲突裁决 |
| | [技能闭环](docs/topics/skills.md) | 成功 run 蒸馏成 runbook，越用越稳 |
| 多 Agent 治理 | [策略与治理](docs/topics/governance.md) | 死循环兜底、消息不丢不重不乱序、权限策略引擎 |
| 可观测与评估 | [全链路追踪](docs/topics/observability.md) | OpenTelemetry 兼容 span，CoT 思维链落盘 |
| | [评估与回归](docs/topics/evaluation.md) | 离线评测 + 线上 Trace 自动打分，LLM-as-judge 校准 |

### 🎯 实战：[9 个端到端示例](docs/examples.md)

每个示例对应一个真实生产场景，教一个完整的机制组合。

### 📚 深度阅读

- [架构全景](docs/architecture.md) — 分层图、数据流、控制流、关键抽象的深度解析
- [设计哲学](docs/design-philosophy.md) — 10 条核心原则，每条配"为什么"和"反例"
- [心智模型](docs/mental-model.md) — 一次调用的完整生命周期，比七站更深入的源码对照
- [设计取舍](docs/decisions.md) — 每个关键决策的"为什么不选另一种"
- [术语表](docs/glossary.md) — 快速查阅
- [API 参考](docs/reference.md) — 自动生成的接口文档

---

## 包目录即学习顺序

```
src/prodagent/
├── base/          ← 基础工具：配置、异常、重试、事件日志
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
├── backends/      ← 端口实现：file/memory/postgres/redis/neo4j
├── mcp/           ← MCP 桥接：stdio/HTTP 外部工具
└── playground/    ← 可视化（叶子节点，被 import-linter 隔离）
```

---

## 社区与贡献

**这个框架的成长需要你。** 无论你是想学习 Agent 系统设计，还是想把它用到自己的项目里，都欢迎参与：

- ⭐ **点个 Star** — 让更多人看到这个项目
- 🐛 **提 Issue** — 发现 bug、有疑问、想要新功能，都可以提
- 🔧 **提 PR** — 从修文档到加核心机制，所有贡献都欢迎
- 💬 **讨论设计** — 对架构有想法？在 Discussion 里聊
- 📝 **写示例** — 你的使用场景就是最好的教程

详见 [贡献指南](CONTRIBUTING.md)。

### 贡献者友好的设计

- **每个模块都有高质量注释**——注释解释的是"为什么"而不是"做什么"
- **1,182 个离线测试**——改代码后跑 `pytest`，30 秒内知道有没有破坏
- **清晰的 Protocol 边界**——加新后端 = 实现一个 Protocol，不动核心
- **import-linter 强制分层**——CI 自动检查依赖方向，不会不小心搞乱架构

---

## 路线图

当前已完成：核心循环、三执行模式、五协作原语、四轴预算、五级压缩、四通道记忆、HITL 审批、崩溃恢复、可观测、MCP 桥接、5 种后端。

未来方向：分布式运行时、流式多 Agent、更多评估指标、插件市场、企业级 RBAC。

详见 [路线图](ROADMAP.md)。

---

## 常见问题

**Q: 这个框架和 LangChain / LangGraph 有什么区别？**
A: LangChain 是工具集，LangGraph 是状态机。prodagent 是一个完整的 Agent 运行时，内建了预算、恢复、审批、权限、可观测等生产机制。更重要的是，prodagent 的代码小到你能从头读到尾，建立完整的心智模型——这是黑盒框架给不了的。

**Q: 我能只用到其中一部分吗？**
A: 可以。每个机制都是独立模块，有清晰的 Protocol 边界。你的项目缺哪块，就搬哪块。比如只想要四轴预算，直接用 `kernel/budget.py`；只想要上下文压缩，直接用 `cognition/context/`。

**Q: 生产环境能用吗？**
A: 能。`production()` 一键开启全套护甲：落盘恢复、span 追踪、HIGH 工具审批、权限策略、LLM 缓存、上下文压缩。后端支持 file（单机）/ postgres（多副本）/ redis（缓存锁）/ neo4j（图）。

更多问题见 [FAQ](FAQ.md)。

---

## 下一步

👉 **[从 5 分钟上手开始 →](docs/start.md)**

或者直接跳进 [架构全景](docs/architecture.md) / [设计哲学](docs/design-philosophy.md) / [第一部分 · 一次调用的生命周期](docs/tour/index.md)。

---

## 许可证

AGPL-3.0-only — 详见 [LICENSE](LICENSE)。

贡献需签署 [CLA](CLA.md)。

---

> **如果你觉得这个框架有价值，请点个 Star ⭐。你的每一个 Star 都是对"好的架构应该被看见"的投票。**
