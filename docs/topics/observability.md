# 可观测

Agent 出了问题，怎么定位是模型不行、prompt 不行、还是工具返回错了？
前提只有一个：事发时记下的数据够不够回答。框架的观测挂在
[工具](../tour/04-tools.md)那站见过的 hook 卡位上，分三层。

## Span：一次行动的统一形状

```python
# src/prodagent/core/observability.py:11 —— 数据形状在 core
# src/prodagent/hooks/audit.py:86 —— 机制在 hooks
span = logger.span(run_id, "tool_call", {"tool": name, "params": ...})
```

`AgentSpan` 带 `trace_id`（一次 `chat()` 一条）、`parent_span_id`
（子 Agent 的 span 挂在父的下面）、采样位和输入载荷。生产形态的
`SpanObserverHooks`（`hooks/bundles/observability.py:22`）订阅全部
生命周期事件，把每一次 LLM 调用、工具执行、spawn、审批都铸成 span，
经 `SpanExporter` 端口导出——默认 file 后端写
`.prodagent/spans.jsonl`，接 Postgres/自己的后端换端口实现即可。

`AuditLogger`（`hooks/audit.py:42`）在导出前过一道可插拔的脱敏钩子
（`scrubber=`），并且**错误 span 不受采样约束**——事故永不抽样。

## 事件流：`chat()` 本来就是流式的

比 span 更早到手的观测面是 `AgentEvent` 流本身：

```python
async for ev in agent.chat_stream("...", session_id="s"):
    match ev:
        case ThinkTokenEvent():  ...   # 模型正在输出的每个词
        case ToolCallStartEvent(): ... # 工具开跑
        case ToolResultEvent():   ...  # 结果（含错误分类）
        case RunSuspendedEvent(): ...  # 等人审批
        case RunCompletedEvent(): ...  # 终态，run 对象在手
```

Playground 的事件卡片、控制台的彩色输出（
`hooks/observers/console.py:40`，环境变量 opt-in——库不抢 stdout）、
你的 Dashboard，消费的是同一个流。**没有为“可观测”单独发明管道，
执行本身就是可观测的**。

## 死循环探测：观测反哺执行

`core/progress.py` 的 `ProgressMonitor` 坐在 think 之前：滑动窗口里
调用指纹重复超阈值即 `InfiniteLoopDetected`。它本质上是观测（模式
识别）直接变成执行约束的例子——看得见的失控才能被拦下的失控。

## 采样与噪声

`sample_rate`（`hooks/audit.py:59`）按 trace 哈希分桶采样——同一趟
调用的 span 要么全在要么全无，不会出现“半截 trace”。控制台与
`TOKEN_UPDATE` 事件里每轮报缓存命中率，命中率长期低迷会告警
（CacheMonitorHooks）：提示缓存是省钱大户，闲置它是白扔钱。

## 取舍

**为什么不用 OpenTelemetry 全家桶？** 曾经有 OTel 导出器，零消费者，
删了（2026-08 大扫除）。span 的形状在 `AgentSpan`、导出在
`SpanExporter` 端口——接 OTel 是写一个三十行的适配器，值得接的那天
再写。**机制进框架，集成按需长**，这个顺序反过来就是依赖地狱。

