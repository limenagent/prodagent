# API 参考

顶层导出面。每个符号懒解析——`import prodagent` 只加载包本身和一个
助手模块。

## 组装与执行

::: prodagent.runtime.agent.Agent

::: prodagent.runtime.config.AgentConfig

## 工具

::: prodagent.tooling.decorator.tool

## 内核（kernel）

::: prodagent.kernel.budget.HardBudget

::: prodagent.kernel.budget.BudgetLedger

::: prodagent.kernel.step.Step

::: prodagent.kernel.bus.HookRegistry

## 类型词汇（kernel.types）

::: prodagent.kernel.types.ExecutionMode

::: prodagent.kernel.types.RunState

::: prodagent.kernel.types.ToolMeta

::: prodagent.kernel.types.SideEffectLevel

::: prodagent.kernel.types.ToolResult

## 协作原语（coordination）

::: prodagent.coordination.ensemble.Ensemble

::: prodagent.coordination.work_queue.WorkQueue

::: prodagent.coordination.blackboard.Board

::: prodagent.coordination.blackboard.Trigger

::: prodagent.coordination.termination.TerminationPolicy

## 记忆（cognition.memory）

::: prodagent.cognition.memory.manager.MemoryManager

::: prodagent.cognition.memory.manager.build_memory_manager

## LLM（llm）

::: prodagent.ports.llm.LLMClient

::: prodagent.ports.llm.LLMConfig

::: prodagent.llm.fake.FakeLLMAdapter

::: prodagent.llm.fake.RoutingFakeLLM

---

完整模块级文档随代码 docstring 演进；这一页只钉**顶层契约面**。
