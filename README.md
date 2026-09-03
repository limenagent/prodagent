# prodagent

> **生产级 LLM Agent 框架——内核小到能读完，完整到能上生产，美到想收藏。**

[![Python](https://img.shields.io/pypi/pyversions/prodagent?logo=python&logoColor=white)](https://pypi.org/project/prodagent/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-1%2C088-offline-green)]()
[![Dependencies](https://img.shields.io/badge/core%20deps-4-purple)]()

**中文** · [English](README.en.md) · 极客时间专栏[《生产级 Agent 排雷实战》](http://gk.link/a/12L6Q)配套框架

---

## 你会用 LangChain，但你能设计一个工业级 Agent 框架吗？

翻一翻 2026 年的 Agent 岗位，分层很清楚。

- **初级岗**："熟练使用 LangChain/LangGraph，会写 prompt，做过 RAG"——三个月上手，供给最卷。
- **高级岗 / 架构师岗**："从 0 到 1 构建生产级 Agent 系统"、"深度定制或自研核心模块"、"多智能体架构设计"——薪资翻两到三倍，能接住的人很少。

面试没人问你"LangChain 的 AgentExecutor 怎么初始化"，他们问的是：

- 一个 `while True` 调模型的循环，上生产之前要加多少层护甲？
- State 为什么是「**通道 + reducer**」，不是普通 `dict`？两个节点并行写同一个键会怎样？
- `kill -9` 之后怎么从断点续跑，不丢状态、不重复执行？
- turns / seconds / tokens / cost 四轴预算怎么同时生效，任一触顶即停？
- 什么时候该拆多 Agent，什么时候单 Agent 加好上下文管理就够了？
- 改了一版 prompt，怎么知道变好还是变差？

这些问题，API 文档给不了答案。这个仓库讲清**如何从 0 设计一个工业级 Agent 框架**，并附带完整参考实现——prodagent。完整的设计文档见 [文档](docs/book/ch00.md)。

---

## 一句话

内核是**七个部件**，其余全是应用层配方：

```
用户应用层（策略）：ReAct / plan-and-resolve / 多 Agent 协作
            │ 用内核原语拼装
内核（机制）：
  Plan = Node + Edge + State 通道    ← 静态蓝图（代码成边）
  Run     一次执行                   ← 状态机 + 父子链
  Scheduler 沿边算就绪、波次推进      ← 唯一引擎
  Interrupt 放手暂停、原样恢复
  Bus       fire / check / collect  ← 对外唯一缝
  Event Log 唯一真相，State = fold(事件)
```

内核不认任何厂商、任何「模式」。ReAct、plan-first、多 Agent 协作，都是**用内核原语在应用层拼出来的配方**——`runtime/recipes/` 里的 `LoopBody`、`react` 就是参考实现。

---

## 三层 API，同一张图

```
L1  prebuilt    ReActAgent / LoopBody —— 一行起，开箱即用
L2  @workflow   @workflow 装饰器 + 顺序/if/while/并行，编译器生成边
L3  裸图        Plan / Node / Edge / Channel —— 手写图
```

三层跑的是**同一个内核**。写 L2 代码，编译成 L3 的图，调度器一行不改。

---

## 30 秒看它能做什么

### 最小可跑（默认就是 ReAct 循环）

```python
import asyncio
from prodagent import Agent, tool

@tool(name="search", readonly=True)
async def search(query: str) -> str:
    return f"results for: {query}"

agent = Agent("demo", system_prompt="Find answers.", tools=[search])

asyncio.run(agent.chat("巴黎今天天气如何？"))
```

### 用 @workflow 把流程写成代码，边由编译器生成

```python
from prodagent import workflow, compile

async def fetch(ctx, s): ...
async def analyze(ctx, s): ...
async def report(ctx, s): ...

@workflow
async def body(ctx, s):
    await ctx.call(fetch)            # 顺序 → 顺序边
    if s.need_deep:                  # if → 条件边
        await ctx.call(analyze)
    await ctx.call(report)           # while → 回边

plan = compile(body).plan            # 编译成 Plan(nodes, edges)
```

### 一键上生产全套护甲

```python
from prodagent import Agent, AgentConfig
from prodagent.base.config import production

agent = Agent("demo", tools=[search],
              config=AgentConfig(name="demo", framework=production()))
```

落盘恢复 + span 追踪 + HIGH 工具审批 + 权限策略 + LLM 缓存 + 上下文压缩，一行切换。

---

## 文档

设计文档（[序章 →](docs/book/ch00.md)）：

| 章 | 主题 |
|---|---|
| 0 | [序章 · 5 分钟跑起来](docs/book/ch00.md) |
| 1 | [全景：内核七部件](docs/book/ch01.md) |
| 2 | [模型层](docs/book/ch02.md) |
| 3 | [循环内核](docs/book/ch03.md) |
| 4 | [预算](docs/book/ch04.md) |
| 5 | [工具系统](docs/book/ch05.md) |
| 6 | [记忆、压缩与技能](docs/book/ch06.md) |
| 7 | [事件日志与崩溃恢复](docs/book/ch07.md) |
| 9 | [审批](docs/book/ch09.md) |
| 10 | [多 Agent 协作](docs/book/ch10.md) |
| 11 | [可观测](docs/book/ch11.md) |
| — | [API 参考](docs/book/api-reference.md) · [附录](docs/book/appendix.md) |

---

## 安装

```bash
pip install prodagent

# 核心仅 4 个依赖，按需加装：
pip install "prodagent[openai,anthropic]"    # 模型 provider
pip install "prodagent[playground]"           # 可视化 playground
pip install "prodagent[postgres,redis,neo4j]" # 生产后端
```

模型配置三选一：

- `USE_FAKE_LLM=1` — 完全离线，学习/测试用
- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` — 任意 OpenAI 兼容端点（DeepSeek/Qwen/Moonshot…）
- `ANTHROPIC_API_KEY` — Anthropic 原生

---

## 示例

`examples/` 下 7 个可跑示例，覆盖各协作方式：

- `greeter` — 最小骨架（Agent + @tool）
- `trader` / `deep_research` / `code_detective` — ReAct 多轮（对话、探索、调试）
- `compliance_audit` — **plan-and-resolve 用内核原语拼装**（plan 节点产清单 → work 节点执行）+ HITL 审批
- `trip_planner` — Workflow 图 + 委派 + 长期记忆
- `aiops` — 工具级审批 + 多工具编排

---

## 许可证

AGPL-3.0-only — 详见 [LICENSE](LICENSE)。贡献需签署 [CLA](CLA.md)。
