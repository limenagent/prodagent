# 测试与评估：用 FakeLLM 做确定性回归

> Agent 的行为是非确定性的，怎么测试？prodagent 的答案是：用 FakeLLM 把模型输出变成预设脚本，让 1,000+ 个测试全离线、全确定性、毫秒级完成。

---

## 问题：Agent 怎么测试？

传统软件的测试是确定性的：给定输入，断言输出。但 Agent 依赖 LLM，而 LLM 的输出是非确定性的——同样的输入可能得到不同的输出。

常见的错误做法：
- 用真实 API 跑测试 → 慢、贵、flaky、有速率限制
- 只测工具函数，不测 Agent 循环 → 核心逻辑没有覆盖
- mock 整个 LLM 调用 → mock 太脆弱，和真实行为脱节

prodagent 的解法：**FakeLLM——一个精确可复现的假模型，让你像写普通单元测试一样测 Agent。**

---

## FakeLLM：确定性的模型替身

```python
from prodagent.kernel.types import LLMResponse, StopReason, ToolCall
from prodagent.llm.fake import FakeLLMAdapter, script

# 预设响应序列：每轮 complete() 消费一个
fake_llm = FakeLLMAdapter(responses=[
    # 第 1 轮：模型决定调用 search
    LLMResponse(
        content="",
        tool_calls=[ToolCall(name="search", params={"query": "巴黎天气"})],
        stop_reason=StopReason.TOOL_USE,
    ),
    # 第 2 轮：模型给出最终答案
    LLMResponse(
        content="巴黎今天晴，25°C。",
        stop_reason=StopReason.END_TURN,
    ),
])
```

`script()` 工厂函数提供更简洁的写法：

```python
fake_llm = script(
    {"tool": "search", "params": {"query": "巴黎天气"}},
    {"content": "巴黎今天晴，25°C。"},
)
```

**FakeLLM 能模拟什么？**
- 多轮工具调用序列（FIFO 队列）
- 流式输出（按词触发 on_chunk）
- 延迟模拟（`latency_ms` 参数）
- 基于消息历史的动态响应（队列中可以放 callable）
- 推理内容（`reasoning_content`）
- 多 Agent 路由（`RoutingFakeLLM` 按 Agent 名称路由不同脚本）

---

## 测试模式

### 模式 1：断言最终输出

```python
@pytest.mark.asyncio
async def test_agent_returns_answer():
    fake_llm = script(
        {"tool": "search", "params": {"query": "巴黎天气"}},
        {"content": "巴黎今天晴，25°C。"},
    )
    agent = Agent(
        "test",
        tools=[search_tool],
        config=AgentConfig(name="test", llm=fake_llm),
    )
    result = await agent.chat("巴黎天气如何？")
    assert "晴" in result.final_text
    assert result.state == RunState.COMPLETED
```

### 模式 2：断言工具调用

```python
@pytest.mark.asyncio
async def test_agent_calls_search():
    called_with = []
    @tool(name="search", readonly=True)
    async def search(query: str) -> str:
        called_with.append(query)
        return "晴，25°C"

    fake_llm = script(
        {"tool": "search", "params": {"query": "巴黎天气"}},
        {"content": "晴"},
    )
    agent = Agent("test", tools=[search], config=AgentConfig(name="test", llm=fake_llm))
    await agent.chat("巴黎天气？")
    assert called_with == ["巴黎天气"]
```

### 模式 3：断言事件流

```python
@pytest.mark.asyncio
async def test_agent_emits_events():
    fake_llm = script({"content": "你好"})
    agent = Agent("test", config=AgentConfig(name="test", llm=fake_llm))

    events = []
    async for event in agent.stream("你好"):
        events.append(type(event).__name__)

    assert "RunCompletedEvent" in events
```

### 模式 4：断言预算

```python
@pytest.mark.asyncio
async def test_budget_exceeded():
    # 每轮都调用工具，永不结束
    fake_llm = FakeLLMAdapter(responses=[
        LLMResponse(
            tool_calls=[ToolCall(name="search", params={"query": "x"})],
            stop_reason=StopReason.TOOL_USE,
        )
    ] * 100)  # 准备 100 轮响应

    agent = Agent(
        "test",
        tools=[search],
        budget=HardBudget(max_turns=3),  # 只允许 3 轮
        config=AgentConfig(name="test", llm=fake_llm),
    )
    result = await agent.chat("...")
    assert result.state == RunState.FAILED
    assert "turns" in str(result.error).lower()
```

### 模式 5：多 Agent 路由

```python
from prodagent.llm.fake import RoutingFakeLLM

fake = RoutingFakeLLM()
fake.add("researcher", [LLMResponse(content="研究结果", stop_reason=StopReason.END_TURN)])
fake.add("writer", [LLMResponse(content="写作结果", stop_reason=StopReason.END_TURN)])
```

---

## 测试覆盖范围

框架的测试套件（173 个测试文件，1,000+ 测试用例）覆盖：

| 模块 | 测试内容 |
|------|---------|
| `kernel/` | Step 生命周期、预算检查、死循环检测、三协议总线 |
| `tooling/` | 参数校验、工具幻觉、只读并行/写串行、超时 |
| `cognition/` | 五级压缩、四通道记忆、冲突裁决、遗忘曲线 |
| `plan/` | DAG 校验、依赖调度、增量重规划、Workflow 编译 |
| `coordination/` | spawn/peer/ensemble/blackboard/work_queue、消息管道 |
| `hooks/` | 审批门、安全 bundle、可观测 bundle |
| `backends/` | file/memory/redis/postgres/neo4j 后端 |
| `llm/` | FakeLLM、OpenAI/Anthropic 适配器、缓存 |
| `runtime/` | Agent 装配、模式选择、checkpoint 恢复 |
| `skills/` | 技能注册、合成、加载 |
| `mcp/` | MCP 协议适配 |
| `approval/` | 审批挂起/通过/拒绝、多副本恢复 |

**所有测试零 API key、零网络、毫秒级完成。**

---

## 回归测试策略

当你修改框架代码时，FakeLLM 让回归测试变得简单：

1. **记录场景** — 用 FakeLLM 预设一个多轮对话脚本
2. **断言关键行为** — 工具调用顺序、最终输出、状态转换、事件
3. **修改代码后重跑** — 如果行为变了，测试会失败

```python
# 回归测试：确保审批拒绝后模型能增量重规划
@pytest.mark.asyncio
async def test_replan_after_rejection():
    fake_llm = script(
        # 第 1 轮：尝试发邮件（HIGH 副作用，会被拒）
        {"tool": "send_email", "params": {"to": "a@b.com", "body": "x"}},
        # 第 2 轮：被拒后改用站内信
        {"tool": "send_message", "params": {"to": "a@b.com", "body": "x"}},
        # 第 3 轮：完成
        {"content": "已通过站内信发送"},
    )
    # ... 配置审批门为自动拒绝 ...
    result = await agent.chat("通知 a@b.com")
    assert result.state == RunState.COMPLETED
    # 验证没有真正调用 send_email
```

---

## 为什么不内建 evaluation 框架？

prodagent 没有内建的 `evaluation/` 模块（没有 LLM-as-judge、没有指标聚合器）。原因：

1. **评估需求高度场景化** — 客服 Agent 的评估标准和代码 Agent 完全不同，框架不该预设
2. **FakeLLM 已经提供了测试基础** — 确定性回归是最有价值的测试，FakeLLM 让它变得简单
3. **评估应该在框架之上构建** — 你可以用 FakeLLM + pytest + 自己的断言库构建评估体系，不需要框架内建

如果你需要 LLM-as-judge 或 A/B 测试，可以在应用层实现：用 FakeLLM 跑确定性场景做回归，用真实模型跑少量场景做质量评估。

---

## 代码定位

| 内容 | 源码位置 |
|------|---------|
| FakeLLMAdapter / script / RoutingFakeLLM | `llm/fake.py` |
| LLMResponse / ToolCall / StopReason | `kernel/types.py` |
| 测试套件 | `tests/`（173 个文件） |
| 测试 fixture（隔离临时目录） | `tests/conftest.py` |
| Agent 入口（chat/stream） | `runtime/agent.py` |

---

## 下一步

- 想写自己的工具？→ [第 ④ 站：工具系统 →](../tour/04-tools.md)
- 想理解预算怎么测？→ [四轴预算专题 →](budget.md)
- 想贡献代码？→ [贡献指南 →](https://github.com/limenagent/prodagent/blob/main/CONTRIBUTING.md)
