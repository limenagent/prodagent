# 审批（HITL）

一个 HIGH 副作用工具要删数据，怎么挂起等人审批？拒绝之后怎么增量
重规划、不推倒重来？第一个问题的答案从工具声明的一个词开始：

```python
@tool(name="place_order", meta=ToolMeta(
    name="place_order", side_effect_level=SideEffectLevel.HIGH))
async def place_order(...): ...
```

`SideEffectLevel.HIGH`。仅此而已——不需要在工具函数里写任何审批逻辑。
生产形态下，分发管道的审批门（第四站的管道第二关）拦下这个调用，
整个 run 进入 `SUSPENDED`，`pending_tool_call` 与 `pending_approval_id`
落进 checkpoint。**没有任何东西被执行**。

## 挂起与恢复的一个来回

```python
run = await agent.chat("买两杯奶茶", session_id="s1")
# → RunState.SUSPENDED，run.pending_approval_id 有值；approval_request_id
#   也写在返回给你的 ToolResult 上（工具层看得见自己被拦）

await agent.submit_approval(run.pending_approval_id, "approve")
# 或 "reject" —— 决定写进 ApprovalGate（hooks/approval/gate.py:30）

run = await agent.chat(resume=True, session_id="s1")
# → 恢复：重放的是被拦下的那一个 ToolCall，不是重新问模型
```

三个细节：

- **审批请求是给人看的格式**。`ContextAwareApprovalFormatter`
  （`hooks/approval/formatter.py:19`）把调用渲染成带参数截断、配置
  diff、生产环境警示的提示——审批的可用性上限是**人读不读得懂要批
  什么**。
- **拒绝不是异常，是输入**。REJECT 后循环继续跑，模型收到“审批被拒”
  的结果，可以改道——合规示例里它把“提交监管”改成“草拟留人复核”，
  就是增量重规划（[⑥ 规划](../tour/06-plan.md)）触发的。
- **先决定后挂起也支持**：人在你 `chat()` 之前就 `submit_decision`
  的场景（`ApprovalGate.evaluate` 的 resume 分支），决定被推迟消费。

## 策略注入口：Gate 卡位

框架不知道什么叫“危险操作该谁批”。它给的是一个卡位：hook 总线的
`Gate.APPROVAL_REQUEST` 检查器可以 veto 任何调用——你的组织规则
（金额阈值、环境白名单、审批人路由）写成检查器挂上去，和注入防御、
合规扫描坐同一班地铁（[消息平面](../tour/07-multiagent.md)的 GATE
卡位是同一机制在跨 Agent 边界的版本）。框架只出机制不出策略，
这在[设计决策](../decisions.md)里有完整的为什么。

跨重启的审批呢？`ApprovalStore` 端口在（`ports/approval.py`），
多副本部署可以把审批单落共享存储。单进程内默认的 `ApprovalGate`
持有内存字典就够了——审批的多数场景，进程活着人就在。

## 取舍

**为什么不是“每次 HIGH 调用弹一个确认框”（同步阻塞等输入）？**
`chat()` 是一次库调用，不是长连接——阻塞在“等人”上会把库的调用方
全部拖进超时泥潭。SUSPENDED 是一等状态而不是阻塞点：挂起即返回，
恢复是新的调用。代价是 UI 层要保存 `session_id` 和审批 id——playground
的审批按钮就是这么做的。

**为什么不按工具名单配置审批而不是按副作用级别？** 名单是集合，
集合会过期的；`SideEffectLevel` 是工具**作者**对性质的声明（这个操作
不可逆吗？），配置是**运维**对阈值的判断（多贵才要人批？）。两件事
两拨人，级别属于作者，veto 属于运维的检查器。

