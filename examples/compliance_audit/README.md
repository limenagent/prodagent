# 合规审计

> 示例 #4 —— 主 agent REACTIVE 多轮对话 + workflow 子 agent + 崩溃恢复 + 幂等写工具，反洗钱可疑交易场景。

主 agent 永远可交互;用户说"审计今天的交易" → 主 agent 调
`spawn_agent` 委派给 `audit_workflow` 子 agent 跑固定 DAG → 拿到 SAR
结果后继续对话(追问某笔交易、要求重审)。DAG 跑完不阻塞对话。

## 本示例展示什么

- **主 agent REACTIVE + workflow 子 agent** —— 主 agent
  `compliance_audit` 是 REACTIVE 对话入口,永远可交互;固定审计 DAG
  降为 `audit_workflow` 子 agent(`.workflow()` 构造),通过
  `spawn_agent` 触发。两者职责分离:主 agent 灵活对话,子 agent 固定流程。
- **`Workflow` + `@wf.step`** —— 把固定审计 DAG
  （extract_transactions → flag_suspicious ‖ enrich_entity → submit_sar）
  写成 Python 代码，编译成 `Plan`，**跳过 LLM planning 调用**。Workflow
  即 plan，工具即步骤。
- **`FileCheckpointStore`** —— 每次 step 状态转换后原子写 checkpoint
  （版本号 + flock）。
- **`FileEventLog`** —— append-only 事件流，记录 PLAN_CREATED /
  STEP_STARTED / STEP_COMPLETED / STEP_FAILED / STEP_REPLANNED。
- **`hybrid_restore`** —— resume 时先重放 event log 重建 plan 状态，
  再用 checkpoint 补齐 run 级字段。
- **`s3 enrich_entity` 带 poison pill** —— poison 装填时，s3 的
  `wf.llm_step` LLM 调用抛 `RuntimeError`，模拟进程被杀；第二次 run
  解除 poison，从 s3 续跑（跳过已完成的 s1/s2）。
- **幂等写工具** —— `submit_to_regulator` 标为
  `enforced_idempotent=True`，崩溃重试不会重复提交 SAR。

## 为什么需要这个

生产 agent 跑长任务（合规审计、数据迁移、批处理）时，崩溃是常
态：进程 OOM、节点重启、网络断、工具 bug。没有 checkpoint 就得从头
跑；有了 checkpoint + event log，只重跑失败的那一步。合规场景尤其
关键——审计到一半挂了，重跑要保留已完成的 LLM 标注（成本可见），
且对外提交 SAR 必须幂等（重试不能给监管方交两份）。

同时，合规审计是对话场景 —— 用户会追问"TX-1002 再深挖"、"换个阈值
重审"、"上次报告再确认一下"。固定 DAG 跑完就死,不能对话;REACTIVE
主 agent 永远在,按需触发 DAG。

| 没有 checkpoint | 有 checkpoint + event log |
|----------------|--------------------------|
| 崩溃后从头跑 | 只重跑失败的 step |
| 长任务不敢跑 | 可以放心跑几小时的任务 |
| 重试可能重复提交 | 幂等键防重复 |
