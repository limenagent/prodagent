# prodagent

> **生产级 LLM Agent 框架——小到能读完，完整到能上生产，美到想收藏。**
>
> 约 2.9 万行 · 14 个包 · 1,300+ 个离线测试 · 核心仅 4 个依赖

[![PyPI](https://img.shields.io/pypi/v/prodagent?color=blue&logo=pypi&logoColor=white)](https://pypi.org/project/prodagent/)
[![Python](https://img.shields.io/pypi/pyversions/prodagent?logo=python&logoColor=white)](https://pypi.org/project/prodagent/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/limenagent/prodagent/ci.yml?logo=github&label=CI)](https://github.com/limenagent/prodagent/actions)
[![Tests](https://img.shields.io/badge/tests-1%2C300%2B-offline-green)]()
[![Dependencies](https://img.shields.io/badge/core%20deps-4-purple)]()

**中文** · [English](README.en.md) · 极客时间专栏[《生产级 Agent 排雷实战》](http://gk.link/a/12L6Q)配套框架

---

## 一句话

**prodagent 是一个你能从头读到尾、在脑子里建立完整心智模型的工业级 Agent 框架。**

它不是又一个黑盒 SDK，也不是几十行的教学玩具。它卡在中间：每一个机制——循环、预算、恢复、审批、权限、可观测、多 Agent 协作——都小到一次读懂，完整到能直接搬上生产。

---

## 你会用 LangChain，但你能设计一个 工业级 Agent 框架吗？

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

这个仓库讲解**如何从 0 设计一个工业级 Agent 框架**，并附带一个完整的参考实现——prodagent。

| 部分       | 章 | 主题                                                                  |
|------------|----|-----------------------------------------------------------------------|
| 全景       | 1  | [从 10 行 demo 到一个工业级框架](docs/book/ch01.md)                   |
| 单个 Agent | 2  | [模型层：Agent 的大脑](docs/book/ch02.md)                             |
|            | 3  | [循环内核：Agent 的心跳](docs/book/ch03.md)                           |
|            | 4  | [预算：烧钱的闸门](docs/book/ch04.md)                                 |
|            | 5  | [工具系统：Agent 的手](docs/book/ch05.md)                             |
|            | 6  | [记忆、压缩与技能](docs/book/ch06.md)                                 |
|            | 7  | [事件日志与崩溃恢复](docs/book/ch07.md)                               |
|            | 8  | [规划与 DAG](docs/book/ch08.md)                                       |
|            | 9  | [审批：不可逆动作的门](docs/book/ch09.md)                             |
| 多 Agent   | 10 | [多 Agent 协作](docs/book/ch10.md)                                    |
| 观测与回放 | 11 | [可观测：运行不再黑箱](docs/book/ch11.md)                             |
|            | 12 | [可回放、可回滚的运行时](docs/book/ch12.md)                           |
| 附录       | —  | [知识点 / 十条原则 / 取舍 / 术语 / 示例 / API](docs/book/appendix.md) |

**[👉 从序章开始，5 分钟跑起来 →](docs/book/ch00.md)**

---

---

## 为什么是 prodagent，而不是别的

市面上的 Agent 项目走两个极端，prodagent 卡在中间：

| | 黑盒框架（LangChain / AutoGen） | 教学玩具（几十行 ReAct） | **prodagent** |
|---|---|---|---|
| 功能完整度 | 高 | 低 | **高** |
| 代码可读性 | 抽象层厚，改不动核心 | 清晰但缺生产机制 | **约 2.9 万行，按学习顺序排列** |
| 预算 / 恢复 / 审批 | 无或绑死云上 | 无 | **内建，默认开启** |
| 核心依赖 | 数十个（间接） | 1-2 个 | **4 个** |
| 测试 | 依赖真实 API | 几乎没有 | **1,300+ 个，全离线可复现** |
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

这次调用内部经过的完整链路（从执行模式到审批门的每一环），见[第 1 章的架构全景图](docs/book/ch01.md)与[第 3 章的循环内核](docs/book/ch03.md)。

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
- **1,300+ 个离线测试**——改代码后跑 `pytest`，30 秒内知道有没有破坏
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
A: 可以。每个机制都是独立模块，有清晰的 Protocol 边界。你的项目缺哪块，就搬哪块。比如只想要[四轴预算](docs/book/ch04.md)，直接用 `kernel/budget.py`；只想要[上下文压缩](docs/book/ch06.md)，直接用 `cognition/context/`。

**Q: 生产环境能用吗？**
A: 能。`production()` 一键开启全套护甲：落盘恢复、span 追踪、HIGH 工具审批、权限策略、LLM 缓存、上下文压缩。后端支持 file（单机）/ postgres（多副本）/ redis（缓存锁）/ neo4j（图）。

更多问题见 [FAQ](FAQ.md)。

---

## 下一步

👉 **[从 5 分钟上手开始 →](docs/book/ch00.md)**

或者从[本书第 1 章的架构全景](docs/book/ch01.md)直接开始。

---

## 许可证

AGPL-3.0-only — 详见 [LICENSE](LICENSE)。

贡献需签署 [CLA](CLA.md)。

---

> **如果你觉得这个框架有价值，请点个 Star ⭐。你的每一个 Star 都是对"好的架构应该被看见"的投票。**
