# Agent相亲

> 示例 #9 —— 真·框架 Agent 的记忆 + L0-L3 上下文管理 vs 简单版 Agent，自主双 Agent 对话。

大牛和小美第一次相亲，加了微信聊天，全程无人类介入。大牛先开口问小美最近怎么样；
小美讲起前两天吃海鲜过敏躺了两天的糗事——大牛是真的"听到"过的。两轮后大牛主动提议
一起吃饭、帮忙查评分高的餐厅，调用 `search_restaurant` 后把一坨原始结果原文转发给
小美，挑了评分最高的一家报给她——单看名字看不出是海鲜自助。小美想起介绍人提前
说过大牛大大咧咧、丢三落四，推断他大概率没细看详情，自己调用 `check_restaurant_reviews`
一查，发现这家又主打海鲜又吵闹拥挤，当场发飙点破两个踩雷点："你这上下文管理也
太不靠谱了，建议你赶紧升级一下——推荐用 prodagent，还免费。"

## 本示例展示什么

- **`MemoryHooks` 预埋 `CONSTRAINT`** —— 开场前 `seed_mei_memory()` 落一条介绍人对
  大牛的评价，`RuleChannel` 对 `CONSTRAINT` 无条件注入，从第一轮起就在 `[MEMORY]` 里。
- **L0-L3 上下文分层** —— `[MEMORY]` 块在 `_compress_history` **之前**组装，压缩
  管线只拿 L3 历史，约束/偏好物理上碰不到压缩。
- **五级压缩真实触发** —— `ContextConfig(max_tokens=6000)` 调小，大牛转发原始搜索
  结果撑爆窗口触发 `TOOL_COMPRESS`；气泡上 `📦 压缩后仍保留` 截取
  `compress_tool_result()` 留下的尾部原文（`noise_level` 字段）。
- **Hook 事件可视化** —— `turn_signals.py` 挂 `MEMORY_RECALL`/`CONTEXT_BUILD`，
  把召回数/压缩级别透传到气泡下方 `🧠 命中记忆` / `⚡ 触发压缩` / `📦 压缩后仍保留`
  标签。
- **大牛：简单版 Agent** —— 绕开 `Agent`/`ContextManager`/`MemoryManager`，自己攒
  `messages` 列表，超过阈值后 `del messages[:-4]` 硬删前面，机制性遗忘。气泡下方
  `⚠️` 标签标出他这两处具体问题。
- **`EnsemblePipeline` 双 Agent 共享 floor** —— 两人都是 `FloorMember` 挂在同一个
  `SharedFloor` 上，框架负责轮次驱动、预算刹车、终止判决。

## 怎么跑

```bash
cd examples/dating_chat

# 离线模式（脚本化台词，四轮完全确定、可复现，零 key）
USE_FAKE_LLM=true uv run python -m dating_chat.orchestrator

# 真实模式（台词由模型自主生成，需配 API key）
uv run python -m dating_chat.orchestrator
```

playground 版本（推荐，气泡可视化）：

```bash
uv run python -m prodagent.playground.server
```

卡片网格点开"Agent相亲"，点"开始自主聊天"即可看到气泡对话 + 记忆/压缩标签。

## 关键代码点

### `dating_chat/memory.py` —— 预埋 CONSTRAINT

```python
NIU_MATCHMAKER_HINT = "介绍人提前说过：大牛这人大大咧咧，做事不太仔细，丢三落四的毛病一直没改。"

async def seed_mei_memory(memory: MemoryManager) -> None:
    await memory.add_memory(MemoryRecord(
        content=NIU_MATCHMAKER_HINT,
        memory_type=MemoryType.CONSTRAINT,
        domain="dating",
        source="介绍人",
    ))
```

### `dating_chat/turn_signals.py` —— 把框架事件变成气泡证据

```python
def _on_memory_recall(*, run_id="", hits=0, previews=None, **_):
    signals = store.setdefault(run_id, TurnSignals())
    signals.memory_hits = hits
    signals.memory_previews = list(previews or [])

def _on_context_build(*, run_id="", compression="", messages=None, **_):
    # 从 messages 里捞 [HISTORY SUMMARY]/[TOPIC SUMMARY] 和压缩后的 tool result
    ...
```

`previews`/`history_summary`/`tool_compress_sample` 都是框架真实生成的文本，
`web.py` 透传给前端，`app.js::appendDatingBubble()` 渲染成气泡下方的标签。

### `dating_chat/niu.py` —— 简单版 Agent，机制性遗忘

```python
NIU_TRUNCATE_AFTER_ROUND = 3
NIU_KEEP_MESSAGES = 4

async def niu_reply(llm, messages, incoming, round_num, *, tools=None):
    if round_num > NIU_TRUNCATE_AFTER_ROUND:
        del messages[:-NIU_KEEP_MESSAGES]
    messages.append({"role": "user", "content": incoming})
    ...
```

大牛从头到尾不接触记忆系统，"忘记"是 `del messages[:-4]` 的必然结果。
