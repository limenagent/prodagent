# 问候

> 示例 #1 —— 最小可跑 Agent。

一个 `Agent` + 一个 `@tool` + `mode=ExecutionMode.REACTIVE`，30 行组装代码，
就是一个能跑的 agent。

## 本示例展示什么

- **`@tool(name="greet", readonly=True)`** —— readonly 工具是最安全的
  LOW 副作用层。
- **`Agent("greeter", system_prompt=..., tools=[greet], mode=ExecutionMode.REACTIVE)`**
  —— 名字 + 系统提示 + 工具列表 + 执行模式，扁平构造一次到位。
- **`mode=ExecutionMode.REACTIVE`** —— 选 REACTIVE 执行模式（边想边做，不预先规划）。
- **零配置观察者** —— 没传 `hooks`，框架自动挂载
  `ConsoleObserverHooks`，终端免费看到完整生命周期事件。
