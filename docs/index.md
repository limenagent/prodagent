# prodagent

**一个约 1800 行、零运行时依赖、能从头读完的 Agent 框架内核**。我们从六行循环出发，把一台
完整的机器一步步推导出来——图、状态、调度器、事件日志、总线、中断。读过一遍，LangGraph、
Google ADK、CrewAI 的内部实现在你眼里就不再是魔法了。

[**English documentation**](en/README.md){ .md-button .md-button--primary }
[**中文文档**](zh/README.md){ .md-button }

[:material-github: GitHub repository](https://github.com/limenagent/prodagent){ .md-button }

---

## 这是什么 / What is this

**中文**：prodagent 不是又一个“大而全”的 Agent 框架，而是一个小到能在周末从头读完的**教学级参照
实现**。它用最少的代码把“图、状态、调度、事件、中断、多 Agent”这套所有框架共用的内核讲清楚——
内核里没有模型、没有 ReAct 特判，所有协作模式都是同一套原语拼出来的。先读它，再读 LangGraph
会轻松很多。从[中文导读](zh/README.md)开始。

**English**: prodagent is not another all-in-one framework. It is a **teaching-grade
reference implementation** small enough to read in a weekend, showing the shared
kernel behind modern agent frameworks — no model and no ReAct special-case inside
the kernel; every collaboration pattern is assembled from one small set of
primitives. Start with the [English guide](en/README.md).

## 五分钟跑起来 / Quick start

```bash
git clone https://github.com/limenagent/prodagent
cd prodagent
pip install -e .

prodagent run            # offline end-to-end flow, fully deterministic
make play                # browser playground: trace / events / state / files / graph
```

All examples and tests run **offline** with a scripted LLM — no API key, no cost,
deterministic results. A real OpenAI-compatible model is one environment variable
away (`OPENAI_API_KEY`) when you want it.
