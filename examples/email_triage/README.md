# 邮件分拣

> 示例 #5 —— REACTIVE 主 agent + workflow 子 agent + HITL 分级审批 + 技能合成。

一个邮件分拣 Agent，把收到的邮件分类，并按类别路由到合适的动作，
副作用分三级，驱动 human-in-the-loop 审批路由。主 agent 是 REACTIVE
对话入口，按需 `spawn_agent` 委派给固定的分拣 workflow 子 agent 跑
DAG；DAG 跑完返回汇总，主 agent 把结果讲给用户。**用户能追问、能纠错、
能不重跑 DAG 直接讨论结果 —— 主 agent 永远可交互。**

## 本示例展示什么

- **REACTIVE 主 agent + workflow 子 agent（via `spawn_agent`）** ——
  `email_triage` 主 agent 是对话入口，REACTIVE 模式。用户说"分拣收件箱"
  → 主 agent 调 `spawn_agent(name="triage_workflow", task=...)` 委派给
  固定的分拣 workflow（`read_inbox` → 4×`classify` ‖ → 4×`route` →
  `summarize`）→ 拿到汇总后继续对话。DAG 跑完不阻塞对话 —— 主 agent
  永远可交互。子 agent 是固定流程，主 agent 是灵活对话 —— 职责分离。
- **`Workflow` + `@wf.step`** —— 把固定分拣 DAG 写成 Python 代码，
  编译成 `Plan`，**跳过 LLM planning 调用**。`allow_replan=False` 让
  DAG 失败就终止，不让 LLM 重写。
- **LLM 驱动的决策点** —— `classify` 和 `summarize` 不是硬编码规则，
  而是 `@wf.step` 函数闭包捕获 LLM client，调 `llm.complete()` 真正分类
  / 汇总。配了 API key 就花真 tokens；fake 模式用按 system prompt 路由
  的 FakeLLM 返回固定 JSON，离线可跑。
- **`wf.tool_step`** —— route 步直接以已注册工具的名字
  （`archive_email` / `mark_read` / `delete_email`）作为 step action。
  PlanExecutor 通过 `ToolDispatcher` 调它们，HIGH side-effect 照样触发
  HITL —— **workflow 不绕过分级审批**。
- **`SideEffectLevel` 三级**驱动 `ApprovalHooks` 路由:
  - **LOW**（`read_inbox`、`classify_email`）—— 只读，不走门禁。
  - **MEDIUM**（`archive_email`、`mark_read`）—— 可逆写，自动批准但审计。
  - **HIGH**（`delete_email`、`forward_external`）—— 不可逆 / 外部爆炸半径，
    **通过共享 `ApprovalGate` 路由到人工审批**。
- **父子共享 `ApprovalGate`** —— 主 agent 和 workflow 子 agent 用同一个
  `ApprovalGate`（`extensions=[ApprovalHooks(gate=shared)]`）。子 agent 的
  HIGH 工具挂起时，`request_id` 落到这个 gate；主 agent
  `submit_approval` 通过同一个 gate 放行。不共享的话父 run 摸不到子
  agent 的挂起请求 —— 共享 gate 是父子 HITL 传播的关键接线。
- **`chat(resume=True)` 续跑挂起会话** —— `chat()` 返回 SUSPENDED 后，
  `submit_approval` 把决策推到 gate，再调 `chat(resume=True, session_id=...)`
  续跑到终态。`resume=True` 不重发 user msg，只把挂起的 `run_id` 跑完，
  并更新 session 的 `last_turn`（COMPLETED），否则下一轮 `chat` 会误以
  为还在 SUSPENDED 复用同一 `run_id` 撞 checkpoint 版本。


## workflow DAG

`triage_workflow` 子 agent 跑的固定 DAG（编译成 `Plan`，跳过 LLM planning）:

```
read_inbox_step
  ├─ classify_eml_001 (LLM) ─ archive_email   (MEDIUM, 自动批准)
  ├─ classify_eml_002 (LLM) ─ mark_read       (MEDIUM, 自动批准)
  ├─ classify_eml_003 (LLM) ─ delete_email    (HIGH, HITL 门禁)
  └─ classify_eml_004 (LLM) ─ archive_email   (MEDIUM, 自动批准)
                            └─ summarize (LLM)
```

- 4 个 `classify_*` 并行（都依赖 `read_inbox_step`，互不依赖）。每个
  classify 步调一次 LLM，读邮件元数据 + 正文，返回分类 JSON。
- 4 个 `route_*` 并行（各自依赖自己的 classify）。route 用 `tool_step`
  静态绑定到 `_ROUTE_TABLE` 里的工具名。
- `delete_email` 触发 `APPROVAL REQUEST` —— fake 模式下 demo 调
  `submit_approval` 自动批准，真实模式下 playground 弹审批框。
- `summarize` 是 terminal step，调一次 LLM 汇总 4 个 route 的结果，它的
  output 成为 `spawn_agent` 返回给主 agent 的结果。
