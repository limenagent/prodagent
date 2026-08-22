# prodagent

> 
> **25,000 行，13 个包，1,182 个离线测试。**
> 一份你**真的能读完**的工业级 LLM Agent 实现。循环、预算、恢复、审批、权限、可观测、评估、多 Agent 协作——每个机制都小到一次读懂，完整到能上生产。

[![PyPI](https://img.shields.io/pypi/v/prodagent)](https://pypi.org/project/prodagent/)
[![CI](https://github.com/limenagent/prodagent/actions/workflows/ci.yml/badge.svg)](https://github.com/limenagent/prodagent/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11+-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

**中文文档** · [English](README.en.md) · 极客时间专栏[《生产级 Agent 排雷实战》](http://gk.link/a/12L6Q)配套框架

---

## 你会用 LangChain，但你能设计一个 Agent 系统吗

翻一翻 2026 年的 Agent 岗位，分层很清楚。

初级岗要求"熟练使用 LangChain/LangGraph，会写 prompt，做过 RAG"——三个月就能上手，所以供给最卷。高级岗和架构师岗的描述就完全不同了："从 0 到 1 构建生产级 Agent 系统"、"深度定制或自研核心模块"、"多智能体架构设计"、"Agent Platform / AI Infra 建设"。薪资翻两到三倍，能接住的人很少。

这些 title 背后，是一套完整的 Agent 系统设计能力。面试的时候，没人会问你"LangChain 的 AgentExecutor 怎么初始化"，他们问的是：

- **Agent 循环与运行时基础**：一个 while True 调模型的循环，上生产之前要加多少层护甲？turns/seconds/tokens/cost 四轴硬预算怎么同时生效，任一触顶即停？子 Agent 花的钱怎么实时汇总到总账？Agent 死循环怎么检测——重复动作比对、cycle detector、还是硬步数上限？长任务跑到一半进程被 kill -9，怎么从断点续跑，不丢状态也不重复执行？checkpoint 怎么做版本控制，并发写入冲突怎么解？
- **RAG 与检索**：你们的 RAG 怎么做的？Agentic RAG 和普通 RAG 有什么本质区别？检索质量怎么保证——切分策略、混合检索、重排序各怎么选？RAG 还是会幻觉，你怎么控？
- **记忆系统**：Agent 的记忆怎么设计？短期、长期、任务、知识这四类记忆怎么分工？长期记忆存什么、怎么召回、冲突怎么裁决？上下文窗口满了和记忆不够，是同一个问题吗？
- **推理与规划**：ReAct、Plan-and-Execute、Reflection 三种范式怎么选？复杂任务怎么拆解？动态 DAG 和静态 Workflow 各自适用于什么场景？规划走不通了，是推倒重来还是增量重规划？
- **工具调用可靠性**：工具幻觉——模型调用不存在的工具、参数传错 schema——怎么防？工具调用前怎么做校验？工具库大了，是静态注册还是动态语义检索？工具调用失败了怎么容错，重试还是换路径？
- **多 Agent 协作**：什么时候该拆多 Agent，什么时候单 Agent 加好上下文管理就够了？委派、接力、投票、共享黑板、工作队列——五种拓扑怎么选？Agent 间通信怎么保证不丢不重不乱序？多个 Agent 互相甩锅进入死循环，怎么兜底？
- **评估与持续迭代**：怎么量化 Agent 做得好不好？改了一版 prompt，怎么知道变好还是变差？离线评测和线上 Trace 自动打分怎么打通？LLM-as-judge 怎么校准才不偏？评测集被污染了怎么发现？
- **企业级落地**：成本怎么控——模型分级路由、语义缓存、prompt 压缩？权限怎么做——RBAC 到角色级够不够，工具和数据的操作级授权怎么设计？可观测性怎么搭——Trace/Log/Metrics 怎么打通，思维链怎么落盘才能事后回放？

这些问题，论文给不了答案，API 文档也给不了 —— 它们问的不是 "怎么做"，而是 "为什么这么做、换一种行不行"。这种工程判断力只能从完整的实现里磨出来，一层一层看懂取舍。

解法其实到处都有 ——issue 讨论里有一条，云厂商的产品文档里有一条，某个开源框架的源码里藏着一条。但太散了，你得自己翻几十万行、自己拼、自己串成一条线。

这个仓库已经帮你串好了。

---

## 为什么是这个仓库，而不是别的

市面上 Agent 框架不少，但大多走两个极端。

一类是**黑盒框架**——LangChain、AutoGen 之流，功能全但抽象层厚，你学会的是它的 API，不是它的设计。出了问题只能翻源码猜，改不动核心。权限、评估、可观测这些企业级特性，要么没有，要么绑死在它们的云上。

一类是**教学玩具**——几十行代码演示 ReAct 循环，看起来清晰，但没有预算、没有恢复、没有审批、没有多 Agent、没有权限、没有可观测，离生产差十万八千里。

prodagent 卡在中间。它是一个**可以 `pip install` 的生产级库**，同时**整个代码base 只有 25,000 行，13 个包，按学习顺序排列**。你可以从头读到尾，每一个机制都能在脑子里建立完整的心智模型。

更重要的是，它的设计是**可拆解的**。预算、审批、崩溃恢复、上下文压缩、多 Agent 治理、权限策略、可观测追踪——每个都是独立模块，有清晰的 Protocol 边界。你的项目缺哪块，就搬哪块，不用引入整个框架。

---

## 学完你能得到什么

### 第一，一套完整的生产级 Agent 设计心智模型

不是零散的知识点，而是从一次 `chat()` 调用开始，经过循环、预算、工具调度、审批、权限校验、检查点、消息平面、上下文压缩、记忆召回、可观测追踪，到最终返回的**完整生命周期**。

文档第一部分用七站走完这条链路，每一站对应源码里的一个包。读完你能在白板上画出整个 Agent 的运行时架构，并且说清每一层为什么存在、去掉会怎样、替代方案是什么。

### 第二，每个机制都能现场调试

整个框架离线可跑。测试不连网，九个示例的每一轮模型行为都是可复现的脚本。

你可以在 `chat()` 的调用链打断点，看一次请求到底经过了多少层；改一个预算参数，看它在哪一轮、为什么停下来；把审批门拆掉，看没有保护的 Agent 会做出什么；把权限策略改成全放行，看越权操作会触发什么。

理解一个机制最深刻的方式是**调试它，不是背它的结论**。

[→ 开始：文档第一部分，七站读穿一次调用的生命周期](https://limenagent.github.io/prodagent/tour/)

![prodagent](docs/images/prodagent.png)

---

## 快速开始

最小可跑，零文件零旁路：

```
import asyncio

from prodagent import Agent, ExecutionMode, tool

@tool(name="search", readonly=True)
async def search(query: str) -> str:
    return f"results for: {query}"

agent = Agent("demo", system_prompt="Find answers.", tools=[search],
              mode=ExecutionMode.REACTIVE)

asyncio.run(agent.chat("巴黎今天天气如何？"))
```

一键上生产全套（落盘恢复、span 追踪、HIGH 工具审批门、权限策略、LLM 缓存、上下文压缩）：

```
from prodagent.core.config import production

agent = Agent("demo", ..., config=AgentConfig(name="demo", framework=production()))
```

可视化 playground，离线跑全部 9 个示例：

```
make playground    # 自动装 uv、首跑配置向导、开浏览器
```

安装与模型配置

```
pip install prodagent
# 核心仅 4 个依赖：anyio/httpx/pydantic/typing-extensions，按需加装：
pip install "prodagent[openai,anthropic]"      # 模型 provider
pip install "prodagent[playground]"             # 可视化
pip install "prodagent[postgres,redis,neo4j]"   # 生产后端（默认 file+memory 零依赖）
```

模型配置三选一：

- `USE_FAKE_LLM=1` 完全离线
- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 指任意 OpenAI 兼容端点（DeepSeek/Qwen/Moonshot/Zhipu…）
- `ANTHROPIC_API_KEY`

**[→ 完整文档：学习路线 · 一次调用的生命周期 · 专题 · 示例](https://limenagent.github.io/prodagent/)**

---

## 核心能力

| 能力 | 解决什么问题 | 源码 |
| --- | --- | --- |
| **裸核默认** | `Agent()` 零文件零旁路起步；`production()` 一键全套护甲 | `core/config.py` |
| **四轴硬预算** | turns/seconds/tokens/cost 任一触顶即停；子 Agent 花销实时汇总到总账 | `core/budget.py`、`coordination/budget_ledger.py` |
| **崩溃恢复** | checkpoint + 乐观版本控制；kill -9 后从断点续跑，不重复执行已完成步骤 | `ports/checkpoint.py`、`backends/file/` |
| **HITL 审批** | HIGH 副作用工具挂起等人；拒绝触发增量重规划，不是推倒重来 | `hooks/approval/` |
| **权限策略引擎** | RBAC + 操作级授权；Agent 身份、工具权限、数据访问三层策略；越权拦截与审计 | `hooks/authorization/` |
| **三执行模式** | REACTIVE / PLAN_FIRST（动态 DAG）/ Workflow（静态 DAG），按任务复杂度选 | `runtime/`、`plan/` |
| **五协作原语** | agents= 委派、peers= 接力、Ensemble / Blackboard / WorkQueue，覆盖主流多 Agent 拓扑 | `coordination/` |
| **统一消息平面** | 一切跨 Agent 通信走同一管道：去重→契约校验→安全→审计→死信，不丢不重不乱序 | `coordination/messaging/` |
| **五级上下文压缩** | 按 token 占比分级牺牲，每级有明确语义损失边界，关键约束不被压缩掉 | `cognition/context/` |
| **四通道记忆** | 规则/实体/精确/语义并行召回 + 冲突裁决 + 遗忘曲线，不是只有向量检索 | `cognition/memory/` |
| **全链路可观测** | OpenTelemetry 兼容的 span 追踪；每轮推理、工具调用、消息穿越自动埋点；Trace/Log/Metrics 三位联动；CoT 思维链落盘 | `hooks/observability/` |
| **评估与回归** | 离线数据集评测 + 线上 Trace 自动打分；支持 LLM-as-judge、代码规则、人工标注；每次改动可跑回归对比 | `evaluation/` |
| **可替换后端** | 14 个 Protocol 端口；file+memory 默认零依赖，Postgres/Redis/Neo4j 按需替换 | `ports/`、`backends/factory.py` |
| **技能闭环** | 成功的 run 蒸馏成 runbook，下次同类任务按需加载，越用越稳 | `skills/` |
| **MCP 桥接** | 外部工具经 stdio/HTTP 接入，不用为每个工具写适配层 | `mcp/` |

---

## 架构

```
graph TD
    A["Agent()"] --> RL["RunLoop"]
    RL --> F["factory.prepare"]
    F --> R["ReactiveLoop<br/>think→decide→execute"]
    F --> P["PlanExecutor<br/>DAG + 断点续跑"]
    R --> D["ToolDispatcher<br/>只读并行/写串行"]
    P --> D
    D --> AUTH["权限策略引擎<br/>RBAC + 操作级授权"]
    AUTH --> APPR["HITL 审批门"]
    R --> L["LLMClient 端口"]
    P --> L
    RL -->|spawn/peers/Ensemble/Board/Queue| M["Crossing 消息平面<br/>去重→契约→安全→审计→死信"]
    subgraph 可选护甲
        H["hooks：审批/权限/可观测"] --- CK["checkpoint/session"]
        COG["压缩/记忆"]
        EVAL["评估/回归"]
    end
    R -.-> H
    M -.-> H
    R -.-> OBS["span 追踪 / CoT 落盘"]
```

包目录即学习顺序：`core → ports → llm → tooling → runtime → plan → coordination → cognition → hooks → skills → evaluation → backends → mcp → playground`。

---

## 九个端到端示例

不是玩具示例，每个都对应一个真实的生产场景，教一个完整的机制组合：

| # | 示例 | 场景 | 教什么 |
| --- | --- | --- | --- |
| 1 | [greeter](examples/greeter) | 最小可跑 Agent | `@tool` + `Agent` + REACTIVE |
| 2 | [trader](examples/trader) | 奶茶代购协商 | 多轮谈判 + 记忆约束 + HIGH 审批 |
| 3 | [deep_research](examples/deep_research) | 探索式研究 | REACTIVE 树 + 五级压缩 + 预算硬上限 |
| 4 | [compliance_audit](examples/compliance_audit) | 金融合规审计 | 动态 DAG + 审批拒绝→增量重规划 + 权限策略 |
| 5 | [code_detective](examples/code_detective) | 自主修 bug | MCP 桥接 + 技能闭环 + 可观测追踪 |
| 6 | [trip_planner](examples/trip_planner) | 旅行规划 | Workflow DAG + 3 peer 并行 + 消息平面 |
| 7 | [aiops](examples/aiops) | 故障应急 | spawn + peer + 技能 + 审批 + 评估全栈 |
| 8 | [dating_chat](examples/dating_chat) | Agent 相亲 | Ensemble 共享会话 + 记忆 A/B 对比 |
| 9 | [quiz_arena](examples/quiz_arena) | 抢答竞赛 | WorkQueue（租约+死信）+ Blackboard + 多租户隔离 |

全部离线可跑（FakeLLM 脚本精确到每轮工具调用），与 1,182 个测试共用同一套机制。

---

## License

AGPL v3，详见 [LICENSE](LICENSE)。

---

**如果这个仓库帮你把 Agent 设计的某个环节想通了，点个 star，让更多卡在同一处的人看到。**