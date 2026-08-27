# 9 个端到端示例

> 从 20 行的 Hello World 到 300 行的多 Agent 辩论，每个示例都可独立运行。

---

## 示例总览

| 示例 | 行数 | 核心演示 | 涉及机制 |
|------|------|---------|---------|
| [greeter](https://github.com/limenagent/prodagent/tree/main/examples/greeter) | ~20 | 最小可跑 Agent | REACTIVE、FakeLLM |
| [deep_research](https://github.com/limenagent/prodagent/tree/main/examples/deep_research) | ~80 | 自主研究任务 | 工具调用、多轮循环、预算 |
| [trip_planner](https://github.com/limenagent/prodagent/tree/main/examples/trip_planner) | ~100 | 先规划后执行 | PLAN_FIRST、DAG、断点续跑 |
| [compliance_audit](https://github.com/limenagent/prodagent/tree/main/examples/compliance_audit) | ~120 | 高风险操作审批 | HITL 审批、副作用分级 |
| [dating_chat](https://github.com/limenagent/prodagent/tree/main/examples/dating_chat) | ~100 | 长对话记忆 | 四通道记忆、上下文压缩 |
| [trader](https://github.com/limenagent/prodagent/tree/main/examples/trader) | ~150 | 多 Agent 接力 | Peer handoff、预算传播 |
| [quiz_arena](https://github.com/limenagent/prodagent/tree/main/examples/quiz_arena) | ~200 | 多 Agent 辩论 | Ensemble、RoundRobin/Moderated |
| [code_detective](https://github.com/limenagent/prodagent/tree/main/examples/code_detective) | ~250 | 多 Agent 协作排查 | Blackboard、Trigger、共享状态 |
| [aiops](https://github.com/limenagent/prodagent/tree/main/examples/aiops) | ~300 | 多 Agent 运维 | WorkQueue、租约、死信、事件溯源 |

---

## 1. greeter — 最小 Agent

**演示**：20 行代码跑通一个 Agent。

```python
from prodagent import Agent, AgentConfig, ExecutionMode, tool
from prodagent.llm.fake import script

@tool(name="greet", readonly=True)
async def greet(name: str) -> str:
    return f"你好，{name}！"

agent = Agent(
    "greeter",
    tools=[greet],
    config=AgentConfig(name="greeter", llm=script(
        {"tool": "greet", "params": {"name": "世界"}},
        {"content": "已经打过招呼了！"},
    )),
)
```

**学到什么**：Agent 构造、@tool 装饰器、FakeLLM、REACTIVE 模式。

---

## 2. deep_research — 自主研究

**演示**：Agent 自主搜索、阅读、总结，多轮工具调用直到完成。

**涉及机制**：
- 只读工具并行执行（多个搜索同时跑）
- 四轴预算防止无限研究
- 上下文压缩处理长搜索结果
- 死循环检测防止重复搜索

---

## 3. trip_planner — 先规划后执行

**演示**：Agent 先输出旅行计划 DAG，再按依赖关系执行。

**涉及机制**：
- PLAN_FIRST 模式
- DAG 依赖调度（查天气 → 订酒店 → 规划路线）
- 并行执行独立步骤
- 断点续跑（中断后已完成的步骤不重复）

---

## 4. compliance_audit — 审批工作流

**演示**：Agent 执行合规审计，高风险操作（删除记录、发送通知）需要人工审批。

**涉及机制**：
- `side_effect_level=HIGH` 工具自动挂起
- `pending_tool_call` 保存待执行调用
- 审批通过后直接执行，不重新问模型
- 审批拒绝后增量重规划

---

## 5. dating_chat — 长对话记忆

**演示**：跨多轮对话记住用户偏好，跨会话持久化。

**涉及机制**：
- 四通道记忆（规则/实体/精确/语义）
- 上下文压缩（长对话自动摘要）
- SessionStore 跨 Run 持久化
- 记忆冲突裁决（新信息覆盖旧信息）

---

## 6. trader — 多 Agent 接力

**演示**：分析师 → 风控 → 交易员的流水线接力。

**涉及机制**：
- Peer handoff（`peers=[...]` 配置）
- HandoffPacket 传递上下文
- BudgetLedger 共享预算
- 每个 Agent 有独立的工具集和系统提示

---

## 7. quiz_arena — 多 Agent 辩论

**演示**：多个 Agent 轮流回答问题，裁判仲裁。

**涉及机制**：
- Ensemble + RoundRobin/Moderated 发言顺序
- Floor 共享发言记录
- TerminationPolicy（MaxRounds）
- 共享 BudgetLedger

---

## 8. code_detective — 黑板协作

**演示**：多个专家 Agent 围绕一个 bug 排查，通过共享黑板协作。

**涉及机制**：
- Blackboard + Trigger 声明式触发
- Board 版本化 KV + 乐观并发
- buzz_in 模式（抢答）
- 事件驱动的松耦合协作

---

## 9. aiops — 工作队列运维

**演示**：告警分类 → 诊断 → 修复的工作队列，支持租约和死信。

**涉及机制**：
- WorkQueue 拉模式任务分发
- 租约超时自动重新入队
- 死信队列处理失败任务
- 可选 EventLog 事件溯源
- 多 Worker 并行处理

---

## 怎么跑示例

```bash
# 克隆仓库
git clone https://github.com/limenagent/prodagent.git
cd prodagent

# 安装（开发模式）
pip install -e ".[dev]"

# 跑示例（默认用 FakeLLM，不需要 API key）
cd examples/greeter
uv run python -c "import asyncio; from greeter.agent import build_greeter_agent; asyncio.run(build_greeter_agent().chat('跟世界打个招呼'))"

# 用真实模型跑
cd ../deep_research
export LLM_API_KEY=sk-...
export LLM_BASE_URL=https://api.deepseek.com
export LLM_MODEL=deepseek-chat
uv run python -c "import asyncio; from deep_research.agent import build_deep_research_agent, DEFAULT_TASK; asyncio.run(build_deep_research_agent().chat(DEFAULT_TASK))"
```

---

## 下一步

- 想理解示例背后的机制？→ [7 站导览 →](tour/index.md)
- 想深入某个生产问题？→ [专题文档 →](index.md)
- 想写自己的示例？→ [贡献指南 →](../CONTRIBUTING.md)
