# 取舍

一个框架是由它拒绝做什么定义的。这一页是全部重要取舍的清单，
每条给一句理由，可深挖的在对应章节展开。挑一条去挑战它，是理解
这个框架最快的方式。

## 机制与策略

| 决策 | 一句话 Why | 深挖 |
|---|---|---|
| 框架不内置任何注入检测正则 | 什么算危险是应用的威胁模型；内置库对每个垂直领域都是错的 | [⑦ 协作](tour/07-multiagent.md) |
| 安全策略经 Gate 卡位用户注入 | 机制进框架（卡位、veto、审计），策略进应用 | [审批](topics/approval.md) |
| 工具重试默认 1 次 | 静默重试可能已生效的副作用是惊吓不是韧性 | [④ 工具](tour/04-tools.md) |
| 框架只铸造幂等键，不执行幂等 | exactly-once 不存在；at-least-once + 接收方幂等才是可兑现的承诺 | [④ 工具](tour/04-tools.md) |

## 并发与一致性

| 决策 | 一句话 Why | 深挖 |
|---|---|---|
| 无框架级锁 | 抢锁冻结预算、放大级联故障；工具返回 RESOURCE_BUSY，把“等还是绕”交还给模型 | [崩溃恢复](topics/recovery.md) |
| 乐观并发（expected_version） | 冲突是例外不是常态，为常态付锁的代价不值 | [崩溃恢复](topics/recovery.md) |
| `BoardVersionConflict` ≠ `VersionConflict` | 棋盘输家隔离 vs 存储并发冲突是两个概念；统一反而让输家触发终止守卫 | [⑦ 协作](tour/07-multiagent.md) |

## 形态与默认

| 决策 | 一句话 Why | 深挖 |
|---|---|---|
| 裸核默认，`None` 即 `None` | 隐式落盘是调试地雷；显式需要才显式配置 | [上手](start.md) |
| `production()` 是一组默认选择而非特性开关阵列 | 选型是框架的专业，翻开关矩阵是用户的负担 | [专题](topics/recovery.md) |
| 默认 REACTIVE，PLAN_FIRST 显式 | 默认路径不该付规划税；值得显式选择的东西就该显式 | [⑤ 循环](tour/05-loop.md) |
| `SAFETY_NET_BUDGET` 不进用户配置 | 防跑飞是循环的正确性，不是用户的预算 | [预算](topics/budget.md) |

## 结构与边界

| 决策 | 一句话 Why | 深挖 |
|---|---|---|
| 包目录=书目录（14 包，学习序） | 阅读路径、依赖方向、教学顺序三者同构 | [学习路线](index.md) |
| 内核 import 链精确钉死 | "轻量"是会红的测试，不是 README 里的形容词；import Agent 不加载多智能体机械 | [学习路线](index.md) |
| `kernel/` 独立成篇（七模块，不 import 能力包） | 循环的读者不该先穿过协作机械；纯度有 CI 测试 | [⑤ 循环](tour/05-loop.md) |
| Step 是原子，循环是策略 | REACTIVE = while 迭代原子；PLAN_FIRST = for-each-DAG 迭代原子——两种执行器共享同一个原子的纪律 | [⑤ 循环](tour/05-loop.md) |
| profile 只允许出现在 compose.py | "production() 打开什么"是一个文件里的清单，不是散落消费现场的 if | [上手](start.md) |
| 能力槽 provide/require 取代扩展扫描 | 插件声明它携带什么，消费者按类型索取——isinstance/hasattr 扫描是字符串协议 | [专题](topics/approval.md) |
| 总线 ≠ 管线（两个概念，共享机械） | fire/collect 扇出与链式短路是对偶不合并；挂载/优先级/三种分发形状（observe/veto/gather）收敛在 kernel/pipeline.py，领域解释以回调注入，messaging 管线独立保留 | [⑦ 协作](tour/07-multiagent.md) |
| AgentRun 不分解成扩展组合 | 全库被读得最多的对象碎片化换不来任何行为；挂起字段簇才是真内聚 | [⑤ 循环](tour/05-loop.md) |
| AgentConfig 保持扁平不分组 | 分组的动因（新能力加字段）已随插件插槽消失；分组是 churn 换美观 | [⑤ 循环](tour/05-loop.md) |
| 不引入统一 Plugin/Kit 协议类 | 三插槽已各有其位（端口/总线/执行器）；再造一个统一 Plugin 接口是给三种合法签名改名，不是新能力 | [⑤ 循环](tour/05-loop.md) |
| 不加 VersionedStore 泛型端口 | conformance 套件已逐后端钉死乐观并发；平行的泛型协议是无强制力的第二套词汇 | [契约](tour/02-ports.md) |
| `backends` 永不出现在模块级导入 | import 一个 Agent 不该拉起一个数据库驱动 | [契约](tour/02-ports.md) |
| 计划是 DAG 不是图灵完备图 | 带循环的计划要模拟才能回答"它会做什么"，可审计性归零 | [⑥ 规划](tour/06-plan.md) |
| Workflow 是代码不是 YAML | 节点引用的是 Agent 对象不是字符串名字；类型检查站在你这边 | [⑥ 规划](tour/06-plan.md) |
| 挂起是一等状态不是阻塞点 | `chat()` 是库调用；阻塞等人会把调用方拖进超时泥潭 | [审批](topics/approval.md) |
| 循环不可编排（无步骤图 DSL） | 检查点的次序是事故换来的不变量，开放编排=让用户重新踩坑 | [⑤ 循环](tour/05-loop.md) |
| 五个协作原语而非通用图引擎 | 拓扑是读出来的（五个名字各有语义），不是拼出来的 | [⑦ 协作](tour/07-multiagent.md) |
| Crossing 载荷永不拍平成 dict | 拍平丢类型，契约校验退化成 schema 猜测 | [⑦ 协作](tour/07-multiagent.md) |

---

> 每条决策的完整推理链——错误怎么发生 → 怎么定位 → 生产级实现 →
> 坦诚边界——是专栏[《生产级 Agent 排雷实战》](http://gk.link/a/12L6Q)
> 的主线叙事。
