# 压缩

上下文跑到第 30 轮塞不下了，丢哪段？语义损失边界在哪？怎么保证
“用户说过不要红色”这种关键约束不被压缩掉？`cognition/context/` 的
答案是把“丢什么”变成分级决策：不同内容、不同的牺牲顺序。

## 三明治与五级

每次 think 之前，`ContextManager.prepare`（`cognition/context/manager.py:142`）
组装的不只是消息列表，是五段三明治：

```
[状态/约束]  [记忆召回]  [技能]  [历史消息]  [提醒]
```

每段独立可控、独立可压缩。压缩按 token 占用比例**分级触发**——
`ContextConfig`（`core/config.py:23`）的四个阈值就是分级线：

| 级 | 触发点（占 max_tokens） | 牺牲什么 | 语义损失 |
|---|---|---|---|
| NONE | < 25% | 无 | 无 |
| TOOL_COMPRESS | ≥ 25% | 大工具结果 → 头尾摘录 + 外溢指针 | 细节 |
| HISTORY_SUMMARY | ≥ 70% | 早期对话 → LLM 总结 | 时序细节 |
| TOPIC_SUMMARY | ≥ 85% | 总结再总结（话题级） | 结构 |
| EMERGENCY | ≥ 92% | 只保最近轮次 | 大部分 |

分级的关键不在“压”，在**每级有明确的语义损失边界**：TOOL_COMPRESS
只动工具结果不动对话（你说过的话不会变成摘要）；HISTORY_SUMMARY 只动
早期不动近期。全压成一锅的“智能压缩”省了 token，也省掉了模型对自己
历史的信任。

## 外溢：压掉的不是丢了

`ToolResultSpillStore`（`cognition/context/spill.py:62`）把完整的大工具
结果存到上下文之外，压缩后的消息里留下预览 + 指针；框架自动挂一个
`read_tool_result` 工具（`tooling/builtin/read_tool_result.py:80`）——
模型发现摘录不够用时，**自己决定**把原文捞回来。

压掉的不是丢了：资料放到桌上够得着的地方，需要时再拿。

## 位置放哪：和缓存的握手

压缩动了历史，提示缓存的命中边界就模糊了。`ContextManager` 维护
`cache_boundary_index`（经 `LLMConfig` 传给适配器）——稳定前缀不参与
压缩。省 token 和省缓存命中是两个方向的优化，握手点就这一处，
[可观测](observability.md)里的缓存命中率监视着它们别打架。

## aux LLM：总结用的另一个模型

总结是后台活，不该抢主轨迹的队列，也常常值得用更便宜的模型。
`ContextManager` 接受独立的 `aux_llm`（`build_context_manager` 经
`resolve_aux_llm` 自动接：离线时返回罐装应答，真 key 时走
`summary_model`）。示例曾经为此手写过包装器——机制进框架后，
那 143 行手写代码就没了存在理由。

## 取舍

**为什么不是“把一切交给长上下文模型”？** 三重账：钱（按 token 计费，
91k 的每轮都在付费）、准（长上下文的中段遗忘是实测现象）、稳
（历史越长，早前幻觉的自我强化越久）。压缩不是长上下文的替代，
是它的节流阀——先用便宜的机制把上下文喂到最相关，再让长窗口兜底。

