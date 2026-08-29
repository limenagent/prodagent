# prodagent · 从 Python 基础，到独立设计工业级 Agent 框架

> **生产级 LLM Agent 框架——小到能读完，完整到能上生产。**
> 约 2.9 万行 · 14 个包 · 1,300+ 离线测试 · 核心仅 4 个依赖

---

## 你会用 LangChain，但你能设计一个 Agent 系统吗？

2026 年的 Agent 岗位分层很清楚：初级岗"熟练使用 LangChain、会写 prompt"，三个月上手，供给最卷；高级岗和架构师岗"从 0 到 1 构建生产级 Agent 系统"，薪资翻两三倍，能接住的人很少。

面试没人问你"AgentExecutor 怎么初始化"，他们问的是：一个 `while True` 调模型的循环，上生产前要加多少层护甲？四轴预算怎么同时生效？进程被 `kill -9` 后怎么断点续跑？模型调了不存在的工具怎么防？

这些问题，论文给不了答案，API 文档也给不了。解法散落在 issue 讨论、云厂商文档、某个框架的源码里——你得自己翻几十万行、自己拼。

**这本书已经帮你串好了。**它讲解**如何从 0 设计一个工业级 Agent 框架**，并附带一个完整的参考实现——prodagent 本体。Python、计算机基础、设计模式不单独设章，哪个模块用到就在那一章讲透。检验标准不是"你记住了"，而是：换你来设计，你知道从哪里下手。

## 全书目录

| 部分       | 章 | 主题                                                             |
|------------|----|------------------------------------------------------------------|
| 全景       | 1  | [从 10 行 demo 到一个工业级框架](book/ch01.md)                   |
| 单个 Agent | 2  | [模型层：Agent 的大脑](book/ch02.md)                             |
|            | 3  | [循环内核：Agent 的心跳](book/ch03.md)                           |
|            | 4  | [预算：烧钱的闸门](book/ch04.md)                                 |
|            | 5  | [工具系统：Agent 的手](book/ch05.md)                             |
|            | 6  | [记忆、压缩与技能](book/ch06.md)                                 |
|            | 7  | [事件日志与崩溃恢复](book/ch07.md)                               |
|            | 8  | [规划与 DAG](book/ch08.md)                                       |
|            | 9  | [审批：不可逆动作的门](book/ch09.md)                             |
| 多 Agent   | 10 | [多 Agent 协作](book/ch10.md)                                    |
| 观测与回放 | 11 | [可观测：运行不再黑箱](book/ch11.md)                             |
|            | 12 | [可回放、可回滚的运行时](book/ch12.md)                           |
| 附录       | —  | [知识点 · 十条原则 · 取舍 · 术语 · 示例 · API](book/appendix.md) |

## 三十秒上手

```python
import asyncio
from prodagent import Agent, AgentConfig, ExecutionMode, tool

@tool(name="search", readonly=True)
async def search(query: str) -> str:
    """搜索网络信息。"""
    return f"results for: {query}"

agent = Agent("demo",
              system_prompt="你是一个 helpful assistant，使用工具回答问题。",
              tools=[search], mode=ExecutionMode.REACTIVE,
              config=AgentConfig(name="demo"))

print(asyncio.run(agent.chat("巴黎今天天气如何？")).final_output)
```

零配置零 API key（默认 FakeLLM，完全离线）：`pip install prodagent` 即装即跑。接真实模型、一键上生产护甲，见[序章](book/ch00.md)。

---

**前置要求**：会 Python 的变量、函数、类，会用命令行。不需要 AI 背景——用到的概念，书里会在你需要的那一刻教。

**👉 [从序章开始，读完你能独立设计一个工业级 Agent 框架 →](book/ch00.md)**
