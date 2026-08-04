# 故障应急

> 示例 #8 —— 全栈组装：多 Agent + peer handoff + 记忆 + 学习 + 可观测 + 审批。

一个 investigator Agent 并行 fan-out 三个只读诊断子 Agent（spawn），
合成结构化 `IncidentReport`，再根据根因是否明确决定 **handoff 给
remediator peer**（横向控制转移）或直接升级 oncall。组装代码
`aiops/agent.py` 约 245 行，不包含任何自写的记忆、学习、可观测、安全层
逻辑——全部由框架提供。

## 本示例展示什么

- **多 Agent fan-out（spawn）** —— `.agents(diagnostic_child_agents())`
  让 investigator 在同一 turn 内并行 spawn 3 个只读诊断子 Agent。子
  Agent 共享父 Agent 的 LLM 客户端，但拥有独立的 system prompt、工具
  权限和预算。
- **peer handoff（横向控制转移）** —— `.peers([remediator_agent()])`
  声明 remediator 为 peer。investigator 调 ``handoff_to_remediator``
  结束自己的 run（COMPLETED），remediator 作为 continuation 接过
  IncidentReport 继续跑。与 spawn 不同，peer 不是子任务返回结果给父，
  而是 "我干完了，你接着干" —— remediator 的输出就是整个 run 的输出。
- **预算熔断** —— `.budget(turns=20, cost_usd=1.0, seconds=1800.0)`，
  轮次 / 成本 / 时间三维硬上限，任一触顶即停。
- **崩溃恢复** —— 文件级 checkpoint + event log，进程中断后可从断点
  恢复执行，避免半写状态。
- **钩子总线** —— 三种协议分层：
  - **Event** —— 纯通知，观察者不阻断流程（`session.start` /
    `tool.call` / `step.completed`）
  - **CheckPoint** —— 阻塞决策，首个 veto 即停（权限校验、内容审计、
    审批门禁）
  - **Injection** —— 数据聚合，收集所有注入器结果（记忆 recall、上下
    文注入）
- **长期记忆** —— 四通道并行 recall（规则 / 实体 / 精确 / 语义）+
  ACT-R 激活衰减模型 + 写时冲突检测。记忆按类型区分（约束 / 事实 /
  偏好 / 事件），带 TTL 和冲突仲裁。
- **自我进化** —— 成功的 session 自动蒸馏为 Skill runbook 写回
  `skills/` 目录，下次同类任务通过 `get_skill(name=...)` 按需加载。
- **人工审批门禁** —— 高危工具执行前暂停等待人工审批，超时自动拒绝。
- **上下文压缩** —— 内置在 `ContextManager` 中，按 token 占用比例触发
  五级压缩（NONE / TOOL_COMPRESS / HISTORY_SUMMARY / TOPIC_SUMMARY /
  EMERGENCY）。
- **可观测 + span 追踪** —— `ConsoleObserverHooks` 零配置自动挂载；
  `SpanObserverHooks` 把每个生命周期事件落盘为 JSONL span。

## 架构

```
investigator (REACTIVE)
  ├── spawn_agent(name='log_analysis',      task='拉日志找 OOM 签名。')
  ├── spawn_agent(name='deploy_correlation', task='查近期部署是否相关。')
  └── spawn_agent(name='metric_anomaly',    task='算 SLO burn rate。')
        ↓ (并行 fan-out, 每个 HandoffPacket)
        log_analysis      → tail_logs, get_pod_status
        deploy_correlation → get_recent_deploys, get_pr_diff
        metric_anomaly    → query_metrics
        ↓ HandoffInterceptor 校验每个 ChildResult
        ↓ (ContractViolationError → 3 次后进 DeadLetterQueue)
  合成 → IncidentReport JSON
  ↓
  根因明确? ──Y──→ handoff_to_remediator(task=<IncidentReport JSON>)
            │     ── investigator run COMPLETED，remediator 作为 peer 继续
            └─N──→ page_oncall（不 handoff）

remediator (PLAN_FIRST peer continuation)
  ↓ 继承 investigator 的 final_output 作为 prior context
  s1: open_incident
  s2: update_incident（写根因 + 回滚目标）
  s3: rollback（HIGH → ApprovalGate）
  s4: check_slo
  s5: update_incident（postmortem，status=mitigated）
```

## 运行

```bash
# 在 prodagent 仓库根目录
uv sync
cd examples/aiops

# 离线模式（不需要 API key，脚本回放完整轨迹，含 peer handoff）
USE_FAKE_LLM=true uv run python -m aiops.agent

# 真实模式（需配置 ANTHROPIC_API_KEY）
uv run python -m aiops.agent
```

输入故障描述（如 `支付服务有告警`），Agent 执行并行诊断 → 定位根因 →
handoff 给 remediator peer → 回滚 → 验证 SLO → 生成事后总结的完整流程。
离线模式使用预设脚本回放，不产生 API 调用。

### 切到生产后端（Postgres / Neo4j / Qdrant / Redis）

默认 file + memory 后端是单机的、零依赖。要试生产后端，在**仓库根目录**跑
`make playground-prod` —— 它会自动用 docker compose 拉起四个服务，再把 playground
切到生产后端（aiops 示例在 playground 里也会跟着切）。

单独跑 aiops（不走 playground）也支持 prod 后端，只要设 `PRODAGENT_BACKEND=prod`
+ 各服务连接 env，参考根 Makefile 的 `playground-prod` target。checkpoint / event /
memory / span 落 Postgres，entity/fact graph 落 Neo4j，cache / lock /
idempotency / approval / DLQ 落 Redis。

## 组装代码结构

`aiops/agent.py` 的 `build_aiops_agent` 分段组织：

1. 运行模式（`USE_FAKE_LLM` 环境变量）
2. 状态目录（memory / experience / sessions / traces）
3. 能力组件（工具注册表 + 技能 + 辅助 LLM）
4. 记忆 + 经验存储
5. Hook bundles（console / span / memory / learning / approval）
6. 持久化（checkpoint + event log）
7. 总装上线（`Agent(...).agents().reactive().budget().extend()`）

分层工具注册表按风险分层：只读诊断（l1）、事件管理（l2）、高危修复
（l3，走审批）、升级。诊断子 Agent 不持有高危工具调用权。

## 关键代码点

### 长期记忆

```python
MemoryManager(
    FileDocumentStore(MEMORY_DIR),
    FileGraphStore(MEMORY_DIR),
    classifier=MemoryClassifier(aux_llm),
    conflict_policy=DefaultConflictPolicy(llm_client=aux_llm),
)
```

### 自我进化

```python
LearningHooks(
    store=experience_store,
    synthesizer=SkillSynthesizer(aux_llm, skills),
    registry=skills,
)
```

成功 session 后台 patch skill runbook：

```
  LEARNING   Skill 'service-alerting-incident-response' patched
  MEMORY     Classify scanned 2 segment(s), wrote 2 (episodic)
```

### 人工审批门禁

```python
ApprovalHooks(gate=ApprovalGate())
```

HIGH 风险工具触发时,``ApprovalGate`` 抛 ``SuspendPendingApproval``,run
持久化为 SUSPENDED。调用方在 out-of-band 收集人类决定后调
``submit_approval`` + ``agent.chat(resume=True, session_id=...)`` 续跑。
脚本/eval 场景里用循环自动批准续跑直到终态。

### peer handoff（横向控制转移）

investigator 找到根因后，不再 `spawn_agent(name='remediator', ...)`（把
remediator 当子任务，结果折回父），而是 `handoff_to_remediator` ——
结束自己的 run，把 IncidentReport 作为 prior output 交给 remediator
peer 继续。语义上更干净：investigator 的工作完成了，remediator 接管。

```python
Agent("investigate", ...)
    .agents(diagnostic_child_agents())              # 诊断 fan-out: spawn_agent
    .peers([remediator_agent(llm=remediator_llm)])  # 修复: handoff_to_remediator
    .reactive()
```

peer 模式下 remediator 不共享父 LLM —— ``_build_peer_agent`` 复制
``spec.llm``。fake 模式把同一个 ``RoutingFakeLLM`` 传给
``remediator_agent``，让它的 "remediator" 队列按 system prompt 命中。
真 LLM 模式传一个独立的 ``create_llm_client()``。

链路上限 ``MAX_PEER_CHAIN = 5``，防止无限 handoff 循环。

### 可观测 + span 追踪

`ConsoleObserverHooks` 零配置自动挂载。在此之上，本示例挂载
`SpanObserverHooks`，将每个生命周期事件落盘为 JSONL span
（`.traces/trace-<nanos>.jsonl`），带 PII 脱敏和头采样：

```python
from prodagent.hooks.bundles.observability import SpanObserverHooks
from prodagent.resilience.observability.audit import AuditLogger, FileSpanExporter

span_exporter = FileSpanExporter(TRACES_DIR / f"trace-{time.time_ns()}.jsonl")
SpanObserverHooks(audit=AuditLogger(exporter=span_exporter))
```

`FileSpanExporter` 惰性打开文件句柄（构造时不创建文件，首次写入才落
盘），每次写入后 flush，崩溃后 on-disk trace 仍可用。`AuditLogger`
默认走 `LogExporter`（Python logging，不落盘），要写 JSONL 必须显式传
`FileSpanExporter`。

span 示例（一次 OOM 诊断 run 产出 ~40 条 span，涵盖 session / loop /
tool / spawn 四类）：

```json
{"span_id": "c207d229…", "trace_id": "3ba774e4…", "run_id": "7746243d…",
 "action": "session_start", "input_payload": {"task": "OOM payment-service"},
 "latency_ms": 0.0, "parent_span_id": null, "sampled": true}
```

## 评测

```bash
uv run python -m evals.runner --smoke      # 单 case 全栈验证
uv run python -m evals.runner --baseline   # 保存基线报告
uv run python -m evals.runner --ci         # 对比基线，回归则 exit 2
```

黄金数据集验证框架编排能力（工具调用顺序、spawn fan-out、JSON 契约），
由独立 LLM judge 评分。judge 需配置 `JUDGE_MODEL` 和 API key。
