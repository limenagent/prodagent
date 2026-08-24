# 上手

零 API key、零外部服务，全程离线。目标：30 分钟内跑通裸核、
打开生产开关、看着 Agent 在 playground 里跑。

## 1 · 安装

```bash
pip install prodagent
```

发布核心刻意做薄——4 个依赖（anyio / httpx / pydantic / typing-extensions）。
按需加装：

```bash
pip install "prodagent[openai]"        # OpenAI 及兼容端点（DeepSeek/Qwen/Moonshot/Zhipu…）
pip install "prodagent[anthropic]"     # Anthropic
pip install "prodagent[playground]"    # 本地可视化 playground
pip install "prodagent[postgres,redis,neo4j]"  # 生产后端驱动
```

## 2 · 第一个 Agent：零文件

```python
import asyncio

from prodagent import Agent, AgentConfig, ExecutionMode, tool

@tool(name="greet", readonly=True)
async def greet(name: str) -> str:
    """按名字打招呼。"""
    return f"Hello, {name}!"

agent = Agent(
    "greeter",
    system_prompt="你是友好的 greeter。用 greet 工具按名字跟用户打招呼。",
    tools=[greet],
    mode=ExecutionMode.REACTIVE,
)

asyncio.run(agent.chat("跟 Alice 打个招呼。"))
```

跑之前先 `cd` 到一个空目录——跑完你会发现目录**还是空的**。这不是省略：
`tests/core/test_bare_kernel.py` 把它作为契约断言（`list(tmp_path.rglob("*")) == []`）。
裸核的 `None` 就是 `None`：不配 session store 就没有会话落盘，不配 checkpoint
就没有断点文件，`submit_approval()` 会直接告诉你没接审批门。

没有配任何 LLM 时，框架按环境变量解析 provider：`USE_FAKE_LLM=1` 离线；
或 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 指向任意 OpenAI 兼容端点；
或 `ANTHROPIC_API_KEY`。

## 3 · 打开生产开关

裸核跑通了，把同一个 agent 换到生产形态——只换 `framework=`：

```python
from prodagent import Agent, AgentConfig, ExecutionMode, tool
from prodagent.core.config import production

agent = Agent(
    "greeter",
    system_prompt="你是友好的 greeter。用 greet 工具按名字跟用户打招呼。",
    tools=[greet],
    mode=ExecutionMode.REACTIVE,
    config=AgentConfig(name="greeter", framework=production()),
)

asyncio.run(agent.chat("跟 Alice 打个招呼。"))
# 这次 `.prodagent/` 下会出现 runs / sessions / events / spans
```

`production()` 做了什么？把 `FrameworkConfig.profile` 翻到 `"production"`
并打开压缩与工具结果外溢。此后每个默认解析点都会变：
session/checkpoint/event log 解析为 file 后端、挂上 span 导出与 HIGH
工具审批门、LLM 包上响应缓存。这些分支**只存在于一个文件**——
`runtime/compose.py`（组装根），"生产形态到底打开了什么"在那里是一份
能从头读到尾的清单，且有测试保证 profile 判断不会散落到别处。

## 4 · 一个带刹车和门的 Agent

```python
from prodagent import Agent, AgentConfig, ExecutionMode, HardBudget, SideEffectLevel, ToolMeta, tool
from prodagent.core.config import production

@tool(name="place_order", meta=ToolMeta(
    name="place_order", side_effect_level=SideEffectLevel.HIGH))
async def place_order(item: str, qty: int) -> dict:
    return {"ordered": item, "qty": qty}

agent = Agent(
    "shopper",
    system_prompt="帮用户下单。确认后调 place_order。",
    tools=[place_order],
    mode=ExecutionMode.REACTIVE,
    budget=HardBudget(max_turns=10, max_cost_usd=0.5, max_seconds=300.0),
    config=AgentConfig(name="shopper", framework=production()),
)

run = asyncio.run(agent.chat("买两杯奶茶"))
print(run.state)                 # RunState.SUSPENDED —— HIGH 工具在等人审
print(run.pending_approval_id)   # 拿着它去批准或拒绝
asyncio.run(agent.submit_approval(run.pending_approval_id, "approve"))
run = asyncio.run(agent.chat(resume=True, session_id=...))
```

注意三件事：预算是你给的就真停（`HardBudget` 四轴任一触顶即 `BudgetExceeded`）；
HIGH 副作用工具把整个 run 挂起成一个可恢复的 SUSPENDED 状态；恢复靠
`session_id`——同一会话续跑。[预算](topics/budget.md) 和
[审批](topics/approval.md) 两章展开。

## 5 · Playground：看着它跑

```bash
make playground     # 自动装 uv、首跑弹配置向导、开浏览器
```

向导二选一：FakeLLM（离线，直接体验全部 9 个示例）或 OpenAI 兼容端点。
Playground 注入的是 production 形态——事件卡片、DAG 图、审批按钮都是真
框架行为，不是演示特效。9 个示例讲什么，见[示例地图](examples.md)。

## 然后去哪

- 读代码：[第一部分 · 一次调用的生命周期](tour/index.md)，七站读完内核。
- 按问题域深入：[第二部分 · 专题](topics/recovery.md)，七个独立专题。
- 跑示例：[示例地图](examples.md)，九个可运行的教材。

---

> 十分钟跑通只是起点。每个机制背后的推理链——为什么这样设计、失败模式
> 是什么——在专栏[《生产级 Agent 排雷实战》](http://gk.link/a/12L6Q)里逐讲展开。
