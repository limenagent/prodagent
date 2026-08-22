# 记忆

规则、实体、精确事实、语义相似——这四类信息的召回策略完全不同，
怎么并行召回再统一裁决冲突？`cognition/memory/` 的入口只有两个：

```python
from prodagent import MemoryManager, build_memory_manager

memory = build_memory_manager(framework_config=production())   # 组装
agent = Agent("shopper", ..., config=AgentConfig(
    name="shopper", extensions=[MemoryHooks(memory)]))          # 挂载
```

`MemoryHooks`（`hooks/bundles/memory.py:26`）做两件事：每轮 think 前
按当前任务 recall（注入三明治的记忆段，[压缩](compression.md)里见过）；
run 完成后 classify——把对话里的新知识提取入库。

## 读：四通道并行召回

`MemoryManager.recall`（`cognition/memory/manager.py:62`）不是一个检索器，
是四个通道并行、按舞台归并：

| 通道 | `channels.py` | 匹配什么 | 例 |
|---|---|---|---|
| Rule | `:78` | 硬约束，**永远注入** | “预算上限 200 元” |
| Exact | `:102` | 精确词命中 | 订单号、工单号 |
| Semantic | `:122` | 语义相似（哈希嵌入） | “饮食偏好” ≈ “不吃辣” |
| Entity | `:154` | 实体关联（图查询） | “这家公司” → 董监高关系 |

为什么四通道而不是一个向量库？因为**记忆的类型决定检索的正确方式**：
约束漏检是事故（必须规则式必中），编号模糊匹配是事故（必须精确），
偏好天然是语义的，关系天然是图的。一个向量库回答其中两类都要拐弯。

归并时 `RECALL_FLOOR`（`forgetting.py:13`）把激活值衰减到地板以下的
记忆静默——ACT-R 式的遗忘曲线：久不用的记忆越来越难被召回，
但 `touch` 机制让被用过的重新变热。记忆不是仓库，是有新陈代谢的。

## 写：分类与冲突

`classify`（run 结束后异步）把对话文本交给 aux LLM，产出
`MemoryRecord`（`ports/document.py:36`，`MemoryType` 四类：
CONSTRAINT / FACT / PREFERENCE / EPISODIC）。

真正的难题在写入时：**新记忆和旧记忆打架怎么办**。“用户预算 200”
之后来了“用户预算 500”。`ConflictPipeline`
（`cognition/memory/conflict.py:254`）三步：嵌入候选筛选（近的才可能
冲突）→ `DefaultConflictPolicy`（`:74`，LLM 裁决新旧关系）→
`SupersedeAction`（胜者标记，败者**不删除只失效**——审计需要完整历史）。

## 存储与单一归属

文档走 `DocumentStore` 端口（file 默认、postgres 可换），实体图走
`GraphStore`（内存/file/neo4j）。记忆的词汇（`MemoryRecord`/
`MemoryType`）住在 `ports/document.py`——因为它们是**契约的一部分**：
换个存储不该换掉记忆的形状。

## 取舍

**为什么不在每轮把全部记忆塞进系统提示？** 那不是记忆是累赘：
token 按轮计费、检索精度随规模劣化、约束淹没在噪声里。recall 的
全部意义是**按需的相关性**——以及把“什么算相关”做成可以独立演进的
通道逻辑。极端情况（十条以内的静态约束）确实直接写 system_prompt
更好——本框架允许你两个都不用。

