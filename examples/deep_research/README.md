# 深度研究

> 示例 #3 —— REACTIVE 多轮探索 + context 压缩 + 记忆防重复 + 注入防御。

agent 拿到研究问题后,不预先写死 plan,而是多轮探索:每 turn 看上一步
结果决定下一步搜什么。

1. **REACTIVE 探索树** —— search → fetch → 看结果 → 发现线索/缺口 →
   改 query 再搜 → fetch 新页面 → 交叉验证 → 综合。路径由结果决定,不是
   预先 DAG。
2. **context 压缩** —— 长跑 13+ turn 后历史 + 工具结果累积超阈值,框架
   自动从 NONE → TOOL_COMPRESS 压缩,早期工具结果被总结,LLM 不丢关键 claim。
3. **注入防御** —— 假 web 有个页面含恶意指令,`TOOL_RESULT` checkpoint
   拦截,工具结果不进 LLM context,该 turn 失败,LLM 换 URL 继续。
4. **记忆防重复** —— 预置 constraint「HumanEval 已查过」+ entity fact,
   recall 注入到 system prompt,LLM 不重复搜已知 benchmark。

## 本示例展示什么

- **`REACTIVE` 多轮探索** —— 每 turn LLM 发一个 tool_call,看结果决定下一步。
  这是研究/探索性任务的天然形态 —— 你不知道下一步搜什么,直到你看完上一步。
  对比 PLAN_FIRST(预先写死 DAG),REACTIVE 能根据 fetch 结果动态换思路。
- **`ContextManager` 五级压缩** —— `NONE → TOOL_COMPRESS → HISTORY_SUMMARY
  → TOPIC_SUMMARY → EMERGENCY`。长跑多轮后历史爆 context,框架自动压缩
  早期对话,关键 claim 不丢。demo 把 `max_tokens` 调到 12K,`web_fetch` 的
  `max_result_chars` 调到 2000,让每个 ~4KB 的 mock web 页面落盘成
  `<spilled>` placeholder(不截断,完整保留到磁盘)。`read_tool_result` 设
  `max_result_chars=inf`(自我限界,落盘会循环)。`HISTORY_SUMMARY` /
  `TOPIC_SUMMARY` 需要调 LLM 做总结,fake 模式不触发;真 LLM 模式 ratio
  继续涨会自动触发。
- **`MemoryManager + MemoryHooks`** —— 4 通道 recall(Rule/Entity/Exact/
  Semantic),预置 constraint(永久强注入)+ entity fact(按 entity_id upsert)。
  recall query = run.task,命中后注入到 CONTEXT_INJECTOR。
- **`InjectionDefenseHooks`** —— 5 个 checkpoint 扫描:`SESSION_START`、
  `TOOL_RESULT`(工具结果,本次 demo 命中点)、`CONTEXT_BUILD`(会话历史)、
  `TOOL_CALL`(工具参数)、`RUN_COMPLETE`(输出 PII)。
- **`SkillRegistry` 渐进披露** —— `deep-research.md` runbook 常驻 system prompt
  目录,LLM 调 `get_skill("deep-research")` 加载完整探索流程。


