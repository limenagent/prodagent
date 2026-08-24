# 术语表

按主题分组的名词卡。**加粗**的是本框架的精确术语；括号内是常见同义词，
帮你从别处的说法对齐过来。

## 执行

- **kernel（内核）**——独立成篇的七个模块（`kernel/`）：类型、事件、
  状态、总线、预算、原子、循环。只被依赖、不依赖任何能力包——
  有 CI 测试钉死。
- **Step（原子）**——能动性的最小单元：一次模型调用 + 至多一轮工具
  执行。REACTIVE 是 while 迭代它，PLAN_FIRST 是按 DAG 迭代它。
- **能力槽（provide / require）**——总线上的类型化登记处：插件声明
  它携带的能力（审批门、记忆管理器），消费者按类型索取。
- **Agent**——声明式的执行单元：系统提示 + 工具 + 模式 + 预算 + 可选
  协作拓扑与生产配置。入口是 `Agent`（`runtime/agent.py`）。
- **run / AgentRun**——一次 `chat()` 调用的全部运行时状态；有
  `run_id`，可挂起、可恢复、可审计。
- **session / ConversationSession**——多轮对话的根；为每轮分配 run_id，
  持有跨轮的消息种子。
- **REACTIVE**——边走边看的循环：think → decide → execute。
- **PLAN_FIRST**——先出 DAG、（可选）人审、再执行的模式。
- **Workflow**——人写的静态 DAG，编译成预设 Plan。
- **hop（跳）**——RunLoop 的一次迭代：一个 Agent 从准备到终态。
  peers 接力 = 一跳交给下一跳。
- **LeafExecutor**——一跳的执行器协议：`ReactiveLoop` 或 `PlanExecutor`。

## 工具

- **ToolMeta**——工具的元数据：副作用级别、超时、幂等标记。
- **SideEffectLevel**——LOW / MEDIUM / **HIGH**（触发审批挂起）。
- **readonly（只读）**——无副作用的工具；批执行时并行。
- **幂等键（idempotency key）**——框架铸造的 `run_id:c{n}`；执行幂等
  是工具的责任。
- **熔断（circuit breaker）**——连续失败的工具进入 OPEN，调用直接
  拒绝；HALF_OPEN 自动探测恢复。

## 形态与恢复

- **profile（bare / production）**——`FrameworkConfig.profile` 的两种
  形态；键控所有默认解析点。
- **`production()`**——一键生产栈（`core/config.py`）。
- **checkpoint**——`AgentRun` 的全量快照，断点续跑的依据。
- **expected_version / VersionConflict**——乐观并发：带版本保存，
  输者收到冲突。
- **HardBudget**——四轴硬预算：turns / seconds / tokens / cost_usd。
- **SAFETY_NET_BUDGET**——裸核的防跑飞底线；不是用户预算。
- **BudgetLedger**——一链一本账：spawn 子代、peer 接力、舞台成员
  共享的记账与预留（`kernel/budget.py`）。
- **HITL（human-in-the-loop）**——人审：HIGH 工具挂起等决定。
- **SUSPENDED**——“等人”的一等运行状态，不是异常。
- **span**——一次行动的观测记录；`trace_id` 串起一次调用。
- **审计（audit）**——落盘的行动流水，错误永不被采样掉。

## 上下文与记忆

- **三明治（sandwich）**——每轮上下文的五段组装：状态/记忆/技能/历史/提醒。
- **五级压缩**——NONE → TOOL_COMPRESS → HISTORY_SUMMARY →
  TOPIC_SUMMARY → EMERGENCY，按 token 占比分级触发。
- **外溢（spill）**——大工具结果存到上下文外，留指针 + `read_tool_result`
  按需捞回。
- **四通道召回**——Rule / Exact / Semantic / Entity 并行检索后归并。
- **ACT-R 衰减**——记忆激活值随时间下降；被使用则回热（touch）。
- **supersede（失效）**——冲突裁决的输家：不删除，标记失效。

## 协作

- **spawn（`agents=`）**——垂直委派：父推任务给子，结果净化后回流。
- **peers（`peers=`）**——横向接力：上一个把控制权转交下一个。
- **Ensemble**——共享会话的多 Agent（轮流/主持人/全员并发）。
- **Blackboard**——版本化共享看板；字段变化触发专家。
- **WorkQueue**——租约任务池；worker 主动领活。
- **Crossing**——一切跨 Agent 边界消息的信封：方向 × 类型 × 类型化载荷。
- **消息平面（messaging plane）**——Crossing 流经的固定卡位管道：
  DEDUPE → ◈ → CONTRACT → ◈ → GATE → AUDIT。
- **死信（dead letter）**——被拒/超限的穿越；恰好记一次，可查可重放。
- **BoardVersionConflict**——看板槽位的乐观并发输家；隔离不掀盘
  （≠ 存储的 `VersionConflict`）。

## 契约

- **端口（port）**——`ports/` 里的 Protocol；实现可替换的契约面。
- **bundle**——自接线的护甲包（hooks/bundles/）：审批、span、记忆、
  学习、控制台。
- **Gate 卡位**——阻塞式检查点，首个 veto 即停；策略的注入口。
- **aux LLM**——后台辅助模型（总结/分类/蒸馏），不占主轨迹。

---

> 术语即概念边界。这些词的精确定义在代码里，专栏
> [《生产级 Agent 排雷实战》](http://gk.link/a/12L6Q)讲它们是怎么
> 被一次次事故磨出来的。
