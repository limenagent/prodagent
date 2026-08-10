# 抢答竞赛场

> 示例 #10 —— 用一场抢答竞赛，串起 `Blackboard` 和 `WorkQueue`
> 这两个最新加入的协作原语。

## 为什么选这个场景

"抢答"这两个字本身就是 Blackboard 的 `buzz_in` 触发模式要做的事——多名候选人
同时符合条件，但**先抢到锁的人才真正开始计算，抢不到的人连想都不会想**（不是
"都算一遍、谁先算完谁赢、输家再取消"）。选一个字面意思就是"抢答"的场景，是想
让这条容易被误解的语义变得不言自明：读完这个 demo 应该不会再把 `buzz_in` 想成
一场"竞速"。

"后台审题"则是天然的抢活干（work stealing）场景：题库需要审核的题目是一批彼此
独立的活儿，审核员谁有空谁去领下一道，不是主持人挨个派发；审核员失联、题目本
身有问题需要被扔掉，也是 `WorkQueue` 与生俱来要处理的两类异常。两段拼在一起，
后台审题的产出（审核通过的题目）直接就是正式抢答的输入——不是两个互不相干的
玩具例子，而是一条完整的"选题 → 上场"流水线。

## 跑起来看什么

```bash
# 离线跑（零 key、用 FakeLLM，便于快速看一遍流程）
USE_FAKE_LLM=1 uv run --package quiz-arena python -m quiz_arena
# 真实 LLM 跑（按 .env 配置的 vendor 调用，选手可能答错）
uv run --package quiz-arena python -m quiz_arena
```

**第一段（WorkQueue，后台审题）**：5 道候选题，2 名审核员。你会看到：

- `q4`/`q5` 缺题干或缺答案，两名审核员都会拒绝——重试到上限后被扔进死信，
  永远不会进入正式抢答（`ItemDeadLetteredEvent`）。
- `flaky_reviewer` 认领 `q2` 后直接"失联"（从不回报）——租约到期后队列把它
  当一次失败处理，重新排队（`ItemRequeuedEvent`），下一次认领才真正审完。
- 最终只有审核通过的题目（`q1`/`q2`/`q3`）会流入第二段。

**第二段（Blackboard，正式抢答）**：3 位选手（小明/小红/小刚），主持人出题、
判分。你会看到：

- `kickoff` 触发器（`keys=[]`，常驻）：主持人出题、评分，只在真有状态变化时
  才写板子——不会对着同一道还没揭晓的题反复重新触发抢答。
- `buzz_in` 触发器：每道题只有一位选手的 `try_contribute` 被真正调用过。脚本
  跑完后会用 `ContestantMember.compute_count` 做一次硬断言：
  **全场选手真正计算的次数之和，必须严格等于已经问出的题数**——如果这个断言
  失败，说明抢答的互斥语义被破坏了（不止是打印出来看着像，是真的会 `assert`）。
- 谁先抢到是真实的协程调度结果，每次运行谁答哪道题可能不一样——这正是"抢答"
  该有的样子，不是刻意做成确定性脚本。

## 跟其他协作原语的关系

`prodagent` 到这个示例为止有五个进程内协作原语，风格差异很大：

| 原语 | 驱动方式 | 本仓库示例 |
|---|---|---|
| `agents=` | push，父 spawn 子、子返回结果 | [aiops](../aiops) |
| `peers=` | push，横向接力、终止当前 run | [aiops](../aiops) |
| `Ensemble`（`ensemble=`） | push，共享 floor 轮流发言 | [dating_chat](../dating_chat) |
| `Blackboard` | 声明式触发，`event` 并发 / `buzz_in` 抢锁 | **quiz_arena（本示例）** |
| `WorkQueue` | pull，谁空闲谁领活，租约 + 死信 | **quiz_arena（本示例）** |

## 代码里值得看一眼的地方

- `review.py`：`QuickReviewer`/`FlakyReviewer` 是纯 Python 的 `Worker`，不接
  LLM——审题是规则判断，不是所有专家都得是 Agent。
- `contestants.py`：`ContestantMember` 手写实现 `BlackboardMember` 协议，而不是
  复用框架自带的 `AgentBlackboardMember`——原因写在模块开头：抢答需要拿到
  `{contestant, question_id, text}` 结构化结果，而不是一段纯文本，框架自带适配
  器的 docstring 也明说了这种场景该直接实现协议。
- `host.py`：`HostMember.try_contribute` 只在真正有状态变化时才返回
  `BoardWrite`——"没变化就返回 `None`" 是避免抢答触发器对着同一道题空转的关键。

## 关于离线 FakeLLM

`USE_FAKE_LLM=1` 时选手用的是零 key 的 `_HintEchoLLM`（`contestants.py` 里
`FakeLLMAdapter` 的一个薄子类），本身没有真实推理能力。`contestants.py` 给
prompt 挂了一段 `[提示：正确答案是 ...]`——跟 `dating_chat` 的
`[导演提示：...]` 是同一套约定：只有"演员"能看到、绝不代表真实推理，纯粹是
为了让离线 demo 有确定的对错可判。

选手用的是 `ExecutionMode.REACTIVE`，这个模式每轮都会在 messages 末尾追加
一条框架自己的 `[STATE]` 记账消息（跟本 demo 无关，`Turn/State/Failures`
之类的运行时元信息）。基类 `FakeLLMAdapter` 的兜底逻辑是"回声最后一条
user 消息"，会被这条 `[STATE]` 消息挡在最后面，导致回声的是 `[STATE]` 而
不是带私密提示的题目 prompt——`_HintEchoLLM` 因此改成倒序扫描全部消息、
找第一条带 `[提示：...]` 的来回声，绕开这个顺序依赖，不需要改动框架的
REACTIVE 循环本身。

接一个真实 `LLM_API_KEY`（去掉 `USE_FAKE_LLM=1`）时这段提示不会被加入 prompt，
选手就是真刀真枪地凭 system prompt 里的擅长领域去答题，可能会答错。

## 跟 playground 网页版的关系

这个示例已接入 `make playground` 的网页 UI。playground 抽象出了一套通用的
多 Agent 事件协议（`MultiAgentEvent` 信封 + `MultiAgentAdapter` 协议），dating_chat
和 quiz_arena 都走这套，将来加更多例子能复用。

- 左栏「参与者」展示本场所有角色：审核员、主持人、选手、触发器。
- 中栏「事件流」按事件类型渲染：抢答气泡、看板写入卡片、认领/完成/重排/死信卡片、阶段分隔横幅。
- 右栏「状态快照」展示当前看板字段或队列计数器。

选 quiz_arena → Start，会看到两阶段流：阶段 1 后台审题（WorkQueue），阶段 2
正式抢答（Blackboard）。跟 CLI 脚本用的是同一个 adapter，行为一致。
