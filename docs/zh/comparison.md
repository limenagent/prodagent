# 概念对照表：从 prodagent 迁移到主流框架

如果你已经用过 LangGraph、Google ADK、CrewAI 或 OpenAI Assistants，这张表能帮你最快建立映射。
它们表面的词汇不同，剥到底层，处理的是同一组问题。

## 核心概念对照

| prodagent | LangGraph | Google ADK | 大致对应 / 备注 |
|---|---|---|---|
| Plan（Node/Edge/Channel） | StateGraph（node/edge/Channel+Reducer） | Agent 树 + 新版 Workflow API | 静态执行结构 |
| Run | thread / invocation | Invocation + Session | 一次具体执行及其状态 |
| Scheduler | Pregel/BSP 执行器 | Runner | 驱动“下一步谁执行”的引擎 |
| 波次 wave | superstep / 超步 | 单次 runner 调度轮 | 一致性与提交边界 |
| Channel + reducer | Channel + Reducer | session.state 增量合并 | 并发写入如何确定地合并 |
| EventLog（事件事实源） | checkpointer（偏状态快照） | Event 事件流 | prodagent 以事件为唯一真相，状态是折叠投影 |
| Outcome.state_delta | 节点 return / Command(update) | EventActions.state_delta | 节点对状态的增量 |
| `Goto` | `Command(goto=...)` | 路由 / transfer_to_agent | 运行时选边、回边、交接 |
| `Send` | `Send(node, arg)` | 动态子任务 | 运行时才知道份数的扇出 |
| Interrupt / resume | `interrupt()` + `Command(resume=)` | 需自行用外部状态实现 | 暂停等人、再从断点继续 |
| Bus（fire/check） | 回调/中间件、LangSmith 观测 | 回调与事件 | 观测、审批、预算等横切能力的挂载点 |
| 子 Run / SubPlanBody | subgraph | sub_agents / AgentTool | 一个节点里递归跑另一张图 |
| call（委派要返回） | 子图作为节点、结果返回 | AgentTool / task 模式 | 父始终在控 |
| transfer（接力不返回） | 图内 `goto` 到另一智能体 | transfer_to_agent | 控制权一去不返 |
| 黑板 blackboard | 共享通道 + 条件边 | 共享 session.state | 谁数据齐谁动 |
| Agent / Workflow 门面 | `create_react_agent` 等预构建 | LlmAgent / Workflow | 内核之外的好用封装 |

## 设计取向的差异（不是优劣，是取舍）

- **LangGraph** 把图、通道、检查点这些底层原语直接暴露给你，控制力最强，生态最全；代价是概念多、
  内核代码量大，新手容易在抽象里迷路。prodagent 用更少的代码把同一套原理讲到“能从头读完”。
- **Google ADK** 上手友好、Agent 树和工具生态完整，较新版本也补齐了 Workflow 能力；它的发展方向
  本身就说明“自主 Agent”和“确定性工作流”会在某个中间地带汇合，prodagent 正是奔着这个中间态设计的。
- **CrewAI / OpenAI 系**用角色、任务、handoff 这些更高层的概念替你把“图”画好，写起来快；当你需要
  精确控制恢复、并发一致性时，底层依然是状态、图与调度。

一句话：**prodagent 不与它们竞争“功能全”，而是做那个最小、可读、可亲手复刻的参照实现。** 先在这里
把内核原理读透，再回去用任何一个框架，你看到的就不再是 API，而是它们共同的骨架。

> 各家框架在持续演进，这张表描述的是概念层面的对应关系，具体 API 以官方文档为准。
