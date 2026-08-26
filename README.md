# prodagent

> **生产级 LLM Agent 框架——小到能读完，完整到能上生产。**

[![PyPI](https://img.shields.io/pypi/v/prodagent?color=blue&logo=pypi&logoColor=white)](https://pypi.org/project/prodagent/)
[![Python](https://img.shields.io/pypi/pyversions/prodagent?logo=python&logoColor=white)](https://pypi.org/project/prodagent/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/limenagent/prodagent/ci.yml?logo=github&label=CI)](https://github.com/limenagent/prodagent/actions)
[![Tests](https://img.shields.io/badge/tests-1%2C182-offline-green)]()
[![Dependencies](https://img.shields.io/badge/core%20deps-4-purple)]()

**中文文档** · [English](README.en.md) · 极客时间专栏[《生产级 Agent 排雷实战》](http://gk.link/a/12L6Q)配套框架

---

## 你会用 LangChain，但你能设计一个 Agent 系统吗？

翻一翻 2026 年的 Agent 岗位，分层很清楚。

初级岗要求"熟练使用 LangChain/LangGraph，会写 prompt，做过 RAG"——三个月上手，供给最卷。高级岗和架构师岗的描述完全不同："从 0 到 1 构建生产级 Agent 系统"、"深度定制或自研核心模块"、"多智能体架构设计"。薪资翻两到三倍，能接住的人很少。

这些要求的背后，是一套完整的 Agent 系统设计能力。面试没人问你"LangChain 的 AgentExecutor 怎么初始化"，他们问的是：

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

**最小可跑，零文件零旁路：**

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

**一键上生产全套护甲（落盘恢复 + span 追踪 + HIGH 工具审批 + 权限策略 + LLM 缓存 + 上下文压缩）：**

```python
from prodagent import Agent, AgentConfig
from prodagent.base.config import production

agent = Agent("demo", tools=[search],
              config=AgentConfig(name="demo", framework=production()))
```

**一次 `chat()` 调用内部经过的完整链路：**

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
| Python 版本 | 3.11 - 3.14 | CI 矩阵全覆盖 |

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

## 下一步

- **[完整文档 →](https://limenagent.github.io/prodagent/)** — 学习路线 · 一次调用的生命周期七站 · 生产问题域深度专题 · 9 个示例
- **[5 分钟上手 →](https://limenagent.github.io/prodagent/start/)** — 跑通第一个 Agent
- **[设计取舍 →](https://limenagent.github.io/prodagent/decisions/)** — 每个关键决策的"为什么不选另一种"
- 极客时间专栏 [《生产级 Agent 排雷实战》](http://gk.link/a/12L6Q) 配套框架

---
