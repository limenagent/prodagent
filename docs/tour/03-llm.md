# ③ 模型 llm

没有 API key 的机器上，这个框架能跑通全部 9 个示例和 1,182 个测试。
这不是附赠功能，是 `llm/` 包的第一设计约束：

```python
# src/prodagent/llm/providers.py:22
def use_fake_llm() -> bool:
    return os.getenv("USE_FAKE_LLM", "").lower() in ("1", "true", "yes")
```

解析顺序在 `create_llm_client`（`llm/factory` 经 `llm/__init__.py` 懒导出）
里是一条直线：**Fake 显式要求 → OpenAI 兼容端点 → Anthropic → Fake 兜底**。
没有 key 时不报错、不要求配置——静默落到 Fake。于是“克隆仓库五分钟后
跑起第一个 Agent”和“CI 里三 个 Python 版本全离线测试”是同一条路径。

## FakeLLM 是一等公民

`llm/fake.py` 有两件工具，值得认真看：

**`script()`（`llm/fake.py:70`）——把一次轨迹写成字面量：**

```python
from prodagent.llm.fake import script

llm = script(
    {"tool": "search", "params": {"query": "weather paris"}},
    {"content": "Paris: 18°C, rain in the afternoon."},
)
```

九个示例的离线剧本全是它写的。测试断言因此可以精确到“模型第二步
调了什么工具”——行为可复现，才谈得上回归。

**`RoutingFakeLLM`（`llm/fake.py:140`）——一个共享实例服务多个 Agent：**

父 Agent 并行 spawn 三个子 Agent 时，单一响应队列会被并发弹空（弹出
顺序不确定）。RoutingFakeLLM 按 system prompt 锚点分路由：`add(name, ...)`
锚定 `# {name} Agent` 头，`add_route(marker, ...)` 锚任意子串；队列项
可以是静态响应（FIFO 弹出）也可以是 `Callable[[MessageList], LLMResponse]`
（常驻应答器，每次都应答，能看消息历史）。没有一个示例再需要手写
这种机制——它们曾经各自复制过一份。

## 缓存与计价：两个透明装饰

- **`CachingLLMClient`（`llm/cache.py:75`）**——包在真客户端外的提示缓存。
  命中返回 `from_cache=True` 的响应，循环记账时跳过计费。仅在
  production 形态下由 `RunContext` 装配（`runtime/runner.py` 经
  `_resolve_llm`）：裸核不做任何静默优化。
- **`llm/pricing.py`**——`LLMConfig.__post_init__` 里，模型价格表查得到
  就自动填入 `cost_per_million_*`。效果：`HardBudget` 的 cost 轴
  开箱即准，不用手工抄价目。查不到的模型计零价——**宁可免费也不谎报**。

## 结构化输出

`llm/structured_output.py` 的 `parse_json_as` 服务于框架内部所有
“请模型回 JSON”的场景：planner 出 DAG、记忆分类、技能蒸馏、总结压缩。
统一走一个解析器意味着统一的失败语义（重试/降级在一处定义），而不是
六处各写一个 `json.loads` + 各自的 try/except。

## 取舍

**不用 LiteLLM/统一网关库做 provider 层？** 那是“快”的答案：
一百个 provider 先接上。本框架只需要三个事实上的标准（fake / OpenAI
兼容 / Anthropic），多一个依赖就多一层不可控的失败面；而且 FakeLLM
作为一等公民的地位会被“provider 抽象”稀释——离线可跑是本框架的
硬约束，不是配置项。

**为什么计价表放在框架里？** 因为预算的 cost 轴离开价格就是装饰。
目录只有几十个模型前缀、错配即零价，维护成本接近零；换来的是
`run.cost_usd` 这个数字在任何 Dashboard 上可直接信。

