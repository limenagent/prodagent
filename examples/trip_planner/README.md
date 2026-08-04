# 行程规划

> 示例 #7 —— Workflow DAG + peer handoff + 长期记忆。

用户说「7 天日本旅行,预算 15000,喜欢拉面和漫画」,agent 在写死的
DAG 里并行 fan-out 3 个 peer 子 agent,合 3 份结果,按天气调整,
输出最终行程表。

## 本示例展示什么

- **`Workflow` + `wf.llm_step`** —— DAG 写死(parse → 3 peer ‖ → merge →
  weather → final),s1/s5/s6/s7 是真 LLM 节点,workflow 编译成 Plan,
  **跳过 LLM planning 调用**。DAG 即 plan,peer 即步骤。
- **`wf.step(peer_agent)`** —— s2/s3/s4 是子 agent,编译成 `spawn_agent`
  step,depends_on s1 → 并行 fan-out。3 个 peer 各自独立 plan/llm/budget,
  通过 `spawn_agent` 委派,task 用 `{{s1.output}}` 把 prefs 喂进去。
- **`MemoryManager + MemoryHooks`** —— 预置 PREFERENCE「用户偏好拉面和漫画,
  住酒店要靠近车站」,recall query = run.task,task 含「拉面」「漫画」
  关键词 → 命中,注入到 peer 的 system prompt,restaurant peer 知道订拉面店。
- **`RoutingFakeLLM`** —— 父 + 3 peer 共享 LLM,按 system prompt 里的
  `# {name} Agent` 分发到 per-agent 队列,避免并发 spawn 在共享队列上 race。

## 为什么需要这个

旅行规划是多领域协作:行程 / 餐厅 / 交通 各自需要专门工具和 prompt,
不是单个 agent 能搞定。但又不是无序的 fan-out —— 有 DAG 依赖
(parse → peer ‖ → merge → adjust → final)。Workflow 写死 DAG,peer agent
处理各领域,Memory 让用户偏好跨 run 持久化。

| 单 agent 硬编码 | Trip Planner |
|----------------|--------------|
| 一个 agent 调所有工具,prompt 膨胀 | 3 个 peer 各管一摊,prompt 精简 |
| 没有依赖顺序,要么全串行要么全并行 | DAG 写死,parse → 并行 → merge |
| 用户偏好每次都要重问 | Memory 预置 PREFERENCE,recall 自动注入 |


