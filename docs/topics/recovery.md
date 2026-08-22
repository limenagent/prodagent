# 崩溃恢复

Agent 跑了 20 轮，进程被 kill -9 了。怎么从断点续跑，不丢状态、
不重复执行？这两个“不”各对应一个机制：断点靠 checkpoint，不重算
靠幂等重放。

## 三份持久化，各管一段

| 端口 | 管什么 | 粒度 |
|---|---|---|
| `SessionStore`（`ports/session.py`） | 会话根：每轮的 run_id、模式、消息种子 | 每轮一存 |
| `CheckpointStore`（`ports/checkpoint.py:23`） | `AgentRun` 全量状态：消息、工具历史、挂起的调用 | 每个关键节点一存 |
| `EventLog` + `PlanEventLog`（`plan/event_log.py:55`） | 计划事件的追加日志：步骤起止、完成、重规划 | 事实流水 |

为什么三份而不是一份大状态？因为它们的**一致性要求不同**：会话可以
丢最后一轮（用户重说一遍），checkpoint 必须精确到挂起的那个调用
（重放错了就重复执行副作用），事件日志只追加不修改（审计依据）。
合并成一份，就要按最严格的要求付最宽泛的代价。

## 乐观并发：expected_version

```python
# ports/checkpoint.py:23 —— 契约原文
async def save(self, run: AgentRun, expected_version: int | None = None) -> None:
    """... ``expected_version`` enables optimistic concurrency: raise
    ``VersionConflict`` if the stored version differs."""
```

没有框架级锁——分布式锁会冻结预算、放大级联故障（见
[设计决策](../decisions.md)）。取而代之的是乐观并发：每次 save 带上
“我基于第 N 版”，库里已是 N+1 就抛 `VersionConflict`，输家自己决定
重读还是放弃。单进程内 `asyncio` 的协作式调度让这足够；跨进程时
file 后端用文件锁兜底原子性，版本号仍然裁决胜负。

## 恢复的三条路径

1. **SUSPENDED 恢复（软）**——审批挂起是设计内的“崩溃”。`resume=True`
   时 `_load_suspended_turn`（`runtime/agent.py`）从 session 读回
   `pending_tool_call`，循环**重放这一个调用**，不重新问模型。模型
   第二次可能改主意——这正是要避免的。
2. **RUNNING 恢复（硬崩溃）**——kill -9 留下的就是 RUNNING 态的落盘。
   `ReactiveLoop._resolve_run` 从 checkpoint 载入：未决的 tool_use 从
   消息尾部剪掉（模型没看到结果就不算发生），从干净边界续跑。
3. **孤儿检测**——新 run_id 撞上库里已存的同 id？`RunIdCollisionError`
   （`runtime/agent.py:18` 导入处）。宁可拒绝也不悄悄续上别人半截的
   状态。

裸核没有这些吗？有接口没有实现：bare 下 checkpoint 是 `None`，恢复
路径全部短路成“重新开始”。进程内多轮（`InMemorySessionStore`）仍然
成立——内存会话的挂起恢复不需要磁盘。**落盘只在你能受益于跨重启时
才付费**。

## 取舍

**为什么不是事件溯源（全量 event sourcing）？** 追加日志我们有一份
（PlanEventLog），但状态重建走 checkpoint 快照而不是重放全部事件：
Agent 的消息历史大、事件重放的副作用边界难划清（哪些工具调用要
重新执行？）。快照 + 恰好追加的审计流水，是恢复确定性和实现复杂度
之间的诚实折中。

**为什么 checkpoint 不做增量（存 diff）？** 消息列表的 diff 语义脆弱
（压缩/重排序之后难对齐），全量 JSON 写入在 file 后端是原子 rename、
在 postgres 是带版本的一行。等真实性能数据显示痛点再优化——目前
没有。

