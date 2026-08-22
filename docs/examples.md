# 实践 · 示例地图

九个示例，每个都有真实场景、离线剧本（FakeLLM）和 playground 入口。
它们不是 demo 片段——是**可运行的教材**：每个示例只教一组概念，
fake 剧本精确到“模型第几步调什么工具”，行为可复现、可断言。

```bash
make playground    # 离线体验全部 9 个；浏览器打开 http://127.0.0.1:8766
```

## 示例 × 章节

| # | 示例 | 场景 | 教什么 | 对应章节 |
|---|---|---|---|---|
| 1 | [greeter](https://github.com/limenagent/prodagent/tree/main/examples/greeter) | 最小可跑 | `@tool` + `Agent` + REACTIVE 三件套，零文件 | [十分钟上手](start.md) |
| 2 | [trader](https://github.com/limenagent/prodagent/tree/main/examples/trader) | 奶茶代购协商 | 多轮谈判 + 记忆约束 + HIGH 工具审批 | [记忆](topics/memory.md)、[审批](topics/approval.md) |
| 3 | [deep_research](https://github.com/limenagent/prodagent/tree/main/examples/deep_research) | 探索式研究 | REACTIVE 探索树 + 五级压缩实战 | [压缩](topics/compression.md) |
| 4 | [compliance_audit](https://github.com/limenagent/prodagent/tree/main/examples/compliance_audit) | 金融合规审计 | 动态 DAG + 审批拒绝 → 增量重规划 | [⑥ 规划](tour/06-plan.md)、[审批](topics/approval.md) |
| 5 | [code_detective](https://github.com/limenagent/prodagent/tree/main/examples/code_detective) | 自主修 bug | MCP stdio 桥接外部工具 + 技能加载 | 技能、mcp |
| 6 | [trip_planner](https://github.com/limenagent/prodagent/tree/main/examples/trip_planner) | 旅行规划 | 静态 Workflow DAG + 3 peer 并行 + 记忆偏好注入 | [⑥ 规划](tour/06-plan.md)、[⑦ 协作](tour/07-multiagent.md) |
| 7 | [aiops](https://github.com/limenagent/prodagent/tree/main/examples/aiops) | 故障应急全栈 | spawn 扇出 + peer 接力 + 技能 + 审批 + 可观测 | 全部 |
| 8 | [dating_chat](https://github.com/limenagent/prodagent/tree/main/examples/dating_chat) | Agent 相亲 | `Ensemble` 共享会话 + 记忆 A/B 对照（框架 Agent vs 手写循环） | [⑦ 协作](tour/07-multiagent.md)、[记忆](topics/memory.md) |
| 9 | [quiz_arena](https://github.com/limenagent/prodagent/tree/main/examples/quiz_arena) | 抢答竞赛 | `WorkQueue`（租约+死信）接 `Blackboard`（版本化抢答） | [⑦ 协作](tour/07-multiagent.md) |

## 建议路线

- **刚读完[十分钟上手](start.md)**：跑 1 → 2（看审批挂起/恢复怎么发生在
  真实对话里）。
- **读完旅程**：跑 6 → 4（两种拿到 Plan 的方式：人写 DAG / 模型动态
  生成+人审+重规划）。
- **读到协作**：跑 8 → 9（追加式地板 vs 版本化看板，两种共享状态的
  写法对照）。
- **要抄作业**：aiops 是全栈参考——但建议最后看，它什么都有，
  教学上不如前八个聚焦。

## 离线剧本怎么读

每个示例的 `fake_llm.py` 是**轨迹的字面量**。读它等于读“这个示例的
每一轮模型在做什么决定”。框架的 `script()` 写单 Agent 剧本，
`RoutingFakeLLM` 按 system prompt 锚点路由多 Agent 共享的剧本
（`llm/fake.py:140`）。这套东西让示例的行为精确可复现——也是
1,182 个测试全离线跑的同一套机制。

---

> 示例是专栏[《生产级 Agent 排雷实战》](http://gk.link/a/12L6Q)的
> 配套习题：每讲对应一个示例，讲完 Why 回来跑 How。
