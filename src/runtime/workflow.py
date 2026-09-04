"""workflow —— 面向使用者的声明式编排门面。

当你要的不是“一个会自己想的 Agent”，而是“一张看得清的流程图”时，用 Workflow：

    wf = Workflow()
    wf.add_node("fetch", fetch_fn)
    wf.add_node("write", writer_agent)        # 节点也可以直接是一个 Agent
    wf.edge("fetch", "write")
    wf.entry("fetch")
    result = await wf.run("任务")

节点函数写 async def fn(input, ctx) 即可，返回值很宽松：裸值=给下游的值，
dict=写共享状态；要控制流程就用本模块的 go / fork / hand_off / wait_human。
未事先声明的状态键会自动补一个 last 通道，所以入门时你不用先学 reducer。

底层仍是 Plan/Node/Scheduler 那台 BSP 引擎，这层只让声明更顺手。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.kernel import (
    Bus,
    InMemoryEventLog,
    InMemoryStore,
    Node,
    Outcome,
    Plan,
    Run,
    Scheduler,
    Send,
    SubPlanBody,
    last,
)
from src.kernel.body import NodeBody, coerce_outcome
from src.runtime.agent import Agent, _AgentHandoff

# —— 节点里用来表达控制流的便捷函数（不必 import 内核的 Outcome/Command）——


def go(target: str, value: Any = None, **state_delta) -> Outcome:
    """跳到/回到某个节点（回边、循环靠它）。"""
    return Outcome.goto(target, value, **state_delta)


def fork(template: str, items: list, *, key: str | Callable | None = None) -> Outcome:
    """动态扇出：把模板节点按 items 实例化多份并行跑（数量运行时才知道）。"""
    sends = []
    for i, item in enumerate(items):
        if key is None:
            inst_key = str(i)
        elif isinstance(key, str):
            inst_key = str(item[key])
        else:
            inst_key = str(key(item, i))
        sends.append(Send(template, item, inst_key))
    return Outcome.fan_out(*sends)


def hand_off(agent: Any, task: str) -> Outcome:
    """接力（transfer）：把控制权交给另一个 Agent，不再返回当前流程。

    直接传 Agent 对象即可“用即绑定”，无需事先登记；内核命令里仍只记录它的名字
    （可序列化），对象绑定放在 Outcome.bindings 这个运行期瞬态字段上。
    只有当你传的是名字字符串时，才需要 wf.handoff_to 先登记目标。
    """
    name = agent.name if hasattr(agent, "name") else str(agent)
    outcome = Outcome.handoff(name, task)
    if hasattr(agent, "plan"):  # 传的是 Agent 对象：记下绑定
        outcome = dataclasses.replace(outcome, bindings={name: agent})
    return outcome


def wait_human(question: str = "", payload: Any = None, *, kind: str = "approval") -> Outcome:
    """在这一点真正停下来等人给回外部输入（审批/补料），随后用 wf.resume 继续。"""
    return Outcome.park(kind, payload, question)


class _FacadeBody:
    """包一层用户函数：把宽松返回值规整成 Outcome，并给新状态键自动补通道。"""

    def __init__(self, fn: Callable, workflow: Workflow, plan_ref: list):
        self.fn = fn
        self.workflow = workflow
        self.plan_ref = plan_ref

    async def run(self, input: Any, ctx) -> Outcome:
        result = self.fn(input, ctx)
        if hasattr(result, "__await__"):
            result = await result
        outcome = coerce_outcome(result)
        plan = self.plan_ref[0]
        for k in outcome.state_delta:  # 未声明的键自动补 last 通道
            if k not in plan.channels:
                plan.channels[k] = last(None)
        if outcome.bindings:  # hand_off(Agent) 用即绑定
            self.workflow._runtime_targets.update(outcome.bindings)
        return outcome


@dataclass
class WorkflowResult:
    output: Any
    state: dict
    run_id: str
    status: str
    metrics: dict
    run: Any = None

    @classmethod
    def _from(cls, run: Run) -> WorkflowResult:
        return cls(
            output=run.final_output,
            state=dict(run.shared),
            run_id=run.run_id,
            status=str(run.state),
            metrics=dict(run.metrics),
            run=run,
        )

    def __str__(self) -> str:
        return str(self.output)


class Workflow:
    def __init__(
        self,
        *,
        model: Any = None,
        tools: Any = None,
        bus: Bus | None = None,
        store: Any = None,
        eventlog: Any = None,
        max_waves: int = 64,
        concurrency: int = 8,
    ):
        self._model = model
        self._tools = tools
        self.bus = bus or Bus()
        self.store = store or InMemoryStore()
        self.eventlog = eventlog or InMemoryEventLog()
        self.max_waves = max_waves
        self.concurrency = concurrency

        self._nodes: dict[str, tuple[Any, dict]] = {}
        self._edges: list[tuple[str, str, Any]] = []
        self._entry: list[str] = []
        self._channels: dict[str, Any] = {}
        self._known_agents: dict[str, Any] = {}  # 作为节点加入的 Agent
        self._runtime_targets: dict[str, Any] = {}  # hand_off(Agent) 运行时用即绑定
        self._extra_handoffs: dict[str, Any] = {}  # handoff_to 显式登记（传名字时才需要）

    # —— 声明 ——
    def channel(self, name: str, reducer: Any) -> Workflow:
        """显式声明状态通道及其合并规则（如 append/add/merge）。"""
        self._channels[name] = reducer
        return self

    def add_node(
        self,
        name: str,
        body: Any,
        *,
        join: str = "all",
        terminal: bool = False,
        template: bool = False,
        timeout: float | None = None,
        retry: Any = None,
    ) -> Workflow:
        self._nodes[name] = (
            body,
            {
                "join": join,
                "terminal": terminal,
                "template": template,
                "timeout": timeout,
                "retry": retry,
            },
        )
        if isinstance(body, Agent):  # 记下它，交接时按名激活其自身运行时
            self._known_agents[body.name] = body
        return self

    # 简短别名，链式更顺。
    def add(self, name: str, body: Any, **opts) -> Workflow:
        return self.add_node(name, body, **opts)

    def edge(self, src: str, dst: str, *, when: Callable | None = None) -> Workflow:
        self._edges.append((src, dst, when))
        return self

    def branch(self, src: str, routes: dict[str, str], *, decide: Callable) -> Workflow:
        """条件分支：decide(state) 返回 routes 的某个键，据此选边。"""
        for key, dst in routes.items():
            self._edges.append((src, dst, lambda s, k=key: decide(s) == k))
        return self

    def entry(self, *names: str) -> Workflow:
        self._entry = list(names)
        return self

    def handoff_to(self, agent: Any) -> Workflow:
        """显式登记一个可接力的 Agent。

        通常不需要：直接在节点里 hand_off(agent_obj, task) 会用即绑定。只有当你
        只能给出目标名字（hand_off("repairer", task)）时，才用这里先登记对象。
        """
        self._extra_handoffs[agent.name] = agent
        return self

    # —— 编译 ——
    def _as_body(self, body: Any, plan_ref: list) -> NodeBody:
        if isinstance(body, Agent):  # Agent 自包含跑自己的模型
            return _FacadeBody(body.as_task(), self, plan_ref)
        if isinstance(body, Plan):  # 裸 Plan 才用同一调度器递归
            return SubPlanBody(body)
        if callable(body):  # 普通函数 -> 宽松包装
            return _FacadeBody(body, self, plan_ref)
        return body  # 已经是内核 body，原样使用

    def _compile(self) -> Plan:
        plan = Plan(channels=dict(self._channels))
        plan_ref = [plan]
        for name, (body, opts) in self._nodes.items():
            plan.add(Node(name, self._as_body(body, plan_ref), **opts))
        for src, dst, when in self._edges:
            plan.edge(src, dst, when=when)
        if self._entry:
            plan.entry = tuple(self._entry)
        return plan

    def _scheduler(self, plan: Plan) -> Scheduler:
        # handoff 目标来自三处：作为节点加入的 Agent、hand_off(对象) 的用即绑定、
        # handoff_to 的显式登记。接力时一律用目标 Agent 自己的运行时。
        # 用 getter 而非快照：hand_off(对象) 的用即绑定在节点执行时才产生。
        on_handoff = _AgentHandoff(
            lambda: {**self._known_agents, **self._runtime_targets, **self._extra_handoffs}
        )
        return Scheduler(
            llm=self._model,
            tools=self._tools,
            bus=self.bus,
            store=self.store,
            eventlog=self.eventlog,
            on_handoff=on_handoff,
            max_waves=self.max_waves,
            concurrency=self.concurrency,
        )

    # —— 运行 ——
    async def run(self, input: Any = None) -> WorkflowResult:
        self._runtime_targets.clear()  # 每次运行重新收集用即绑定
        plan = self._compile()
        task = ""
        run = Run.start(plan, task=task)
        if isinstance(input, dict):  # dict 作为初始共享状态
            for k, v in input.items():
                plan.channels.setdefault(k, last(None))
                run.shared[k] = v
        elif isinstance(input, str):
            run.task = input
        scheduler = self._scheduler(plan)
        await scheduler.drive(plan, run)
        return WorkflowResult._from(run)

    async def resume(self, run_id: str, value: Any = None) -> WorkflowResult:
        plan = self._compile()
        scheduler = self._scheduler(plan)
        run = await scheduler.resume(plan, run_id, value)
        return WorkflowResult._from(run)
