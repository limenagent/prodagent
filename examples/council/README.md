# Council —— agent 自己召集协作

主持人 agent 声明了一个评审组（ensemble）和一个杂务队列（work queue），
然后**模型自己决定**什么时候开会、什么时候派活——和调 `spawn_agent` 是同一种体验：

```python
agent = Agent(
    "council",
    config=AgentConfig(
        name="council",
        ensembles=[panel],      # → 自动生成 run_ensemble 工具
        work_queues=[chores],   # → 自动生成 run_work_queue 工具
    ),
)
```

- `run_ensemble(name, task)`：成员在共享 floor 上轮流表态，模型拿回完整 transcript；
- `run_work_queue(name, items)`：工人按租约认领、失败重试、超限死信，模型拿回完成清单。

成员和工人都是普通 `Agent`（`AgentFloorMember` / `AgentWorkMember` 适配），
执行统一走 RunnerPort。

## 运行

```bash
cd examples/council
uv sync
uv run python -m council "要不要周五上线"
```

默认 FakeLLM 离线可跑；接真模型把 `FakeLLMAdapter()` 换成 `resolve_llm(...)` 即可。
