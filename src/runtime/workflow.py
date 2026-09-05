"""workflow —— 面向使用者的声明式编排门面。

当你要的不是“一个会自己想的 Agent”，而是“一张看得清的流程图”时，用 Workflow：

    wf = Workflow()
    wf.add_node("fetch", fetch_fn)
    wf.add_node("write", writer_agent)        # 节点也可以直接是一个 Agent
    wf.edge("fetch", "write")
    wf.entry("fetch")
    result = await wf.run("任务")

节点函数写 async def fn(input, ctx) 即可，返回值很宽松：裸值=给下游的值，
dict=写共享状态；要控制流程就用本模块的 go / send / wait_human（go 到另一个
Agent 节点、且不画回边，就是“交出去不回头”的接力；要并行多份就返回一组 send）。
未事先声明的状态键会自动补一个 last 通道，所以入门时你不用先学 reducer。

底层仍是 Plan/Node/Scheduler 那台 BSP 引擎，这层只让声明更顺手。
"""

from __future__ import annotations

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
    SubPlanBody,
    last,
)
from src.kernel.body import NodeBody, coerce_outcome
from src.runtime.agent import Agent

# —— 节点里用来表达控制流的便捷函数（不必 import 内核的 Outcome/Command）——


def go(target: str, value: Any = None, **state_delta) -> Outcome:
    """转场到某个节点（回边、循环、交接都靠它）；value 是喂给目标这一次的输入。"""
    return Outcome.goto(target, value, **state_delta)


def send(template: str, payload: Any, key: str | None = None) -> Outcome:
    """运行时实例化一份模板节点、把 payload 喂给它（就是内核的 Send）。

    要一次扇出多份并行，就返回一组：return [send("w", x) for x in items]，
    具体几份运行时才知道也没关系，引擎会把它们放进同一波里并发跑。
    """
    return Outcome.send(template, payload, key)


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
        return Scheduler(
            llm=self._model,
            tools=self._tools,
            bus=self.bus,
            store=self.store,
            eventlog=self.eventlog,
            max_waves=self.max_waves,
            concurrency=self.concurrency,
        )

    # —— 运行 ——
    async def run(self, input: Any = None) -> WorkflowResult:
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
