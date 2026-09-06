# prodagent

**A minimal, readable agent-framework kernel in about 1800 lines, with zero
runtime dependencies.** Start from a six-line agent loop and see how a complete
machine is derived, one part at a time — graph, state, scheduler, event log, bus,
and interrupt. Read this once and the internals of LangGraph, Google ADK, and
CrewAI stop looking like magic.

[**English documentation**](en/README.md){ .md-button .md-button--primary }
[**中文文档**](zh/README.md){ .md-button }

[:material-github: GitHub repository](https://github.com/limenagent/prodagent){ .md-button }

---

## 这是什么 / What is this

**中文**：prodagent 不是又一个“大而全”的 Agent 框架，而是一个小到能在周末从头读完的**教学级参照实现**。
它用最少的代码把“图、状态、调度、事件、中断、多 Agent”这套共同内核讲清楚——内核里没有模型、没有
ReAct 特判，所有协作模式都是同一小把原语拼出来的。先读它，再读 LangGraph 会轻松很多。
从[中文导读](zh/README.md)开始。

**English**: prodagent is not another all-in-one framework. It is a **teaching-grade
reference implementation** small enough to read in a weekend, showing the shared
kernel behind modern agent frameworks — with no model and no ReAct special-case
inside the kernel; every collaboration pattern is assembled from one small set of
primitives. Start with the [English guide](en/README.md).

## 五分钟跑起来 / Quick start

```bash
git clone https://github.com/limenagent/prodagent
cd prodagent
PYTHONPATH=. python examples/graph_demo.py   # no API key, fully offline
make play                                     # a stdlib-only web playground
```

All examples and tests run **offline** with a scripted LLM — no API key, no cost,
deterministic results. A real OpenAI-compatible model is one environment variable
away when you want it.
