"""agent —— 面向使用者的“智能体”门面（机制在内、好用在外）。

内核只认识 Plan/Node/Scheduler 这些零件；日常使用时，你想面对的是“一个有名字、
有模型、有工具、能派活给同伴、也能把活儿交接出去的 Agent”。这层门面就负责把
零件按最常见的方式组装好：

    researcher = Agent(name="researcher", model=llm,
                       instruction="你负责查资料", tools=[search])
    result = await researcher.run("帮我查一下 X")
    print(result.output)

关键定位：**每个 Agent 自带模型、工具和运行时，自成一个运行单元。**
- tools 直接传普通函数，schema 自动推断；
- teammates 是“派活给它、它干完把结果交回来”的子 Agent（call / agent-as-tool）；
- “交出去不回头”的接力（transfer）属于图编排：在 Workflow 里把各 Agent 当节点，
  用 go(目标Agent, 交接摘要) 转场，不画回边就是不回头，不需要专门的交接命令；
- context / memory 是可选横切策略，不传也能跑。

一个 Agent 调用另一个 Agent，本质是它的某个节点 body 里又跑起同一套内核——
递归的是内核机制本身，不要求共用同一个 Scheduler，于是每个 Agent 用自己的模型，
互不串台。想看零件怎么拼，回到 runtime.react / runtime.multiagent。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.kernel import (
    Bus,
    InMemoryEventLog,
    InMemoryStore,
    Plan,
    Scheduler,
)
from src.runtime.react import build_react_plan, start_react_run
from src.runtime.tools import ToolRegistry, ToolSpec

_TASK_PARAM = {
    "type": "object",
    "properties": {"task": {"type": "string", "description": "交给该子 Agent 的任务"}},
    "required": ["task"],
}


@dataclass
class AgentResult:
    """一次 Agent 运行的结果，output 是给用户看的最终答复，其余供调试。"""

    output: Any
    messages: list
    state: dict
    run_id: str
    status: str
    metrics: dict
    run: Any = None

    @classmethod
    def _from(cls, run: Any) -> AgentResult:
        return cls(
            output=run.final_output,
            messages=list(run.shared.get("messages", [])),
            state=dict(run.shared),
            run_id=run.run_id,
            status=str(run.state),
            metrics=dict(run.metrics),
            run=run,
        )

    def __str__(self) -> str:
        return str(self.output)


class Agent:
    def __init__(
        self,
        name: str,
        *,
        model: Any = None,
        instruction: str = "",
        description: str = "",
        tools: list | None = None,
        teammates: list[Agent] | None = None,
        context: Any = None,
        memory: Any = None,
        registry: ToolRegistry | None = None,
        bus: Bus | None = None,
        store: Any = None,
        eventlog: Any = None,
        write_needs_approval: bool = True,
    ):
        self.name = name
        self.model = model
        self.instruction = instruction
        # description 是给“主管模型”挑下属时看的能力说明，缺省取指令首句。
        self.description = description or (instruction.splitlines()[0] if instruction else name)
        self.context = context
        self.memory = memory
        self.bus = bus or Bus()
        self.store = store or InMemoryStore()
        self.eventlog = eventlog or InMemoryEventLog()

        # 可传入已建好的注册表（例如已挂 MCP 工具）；否则自建一个。
        self._registry = registry or ToolRegistry(
            bus=self.bus, write_needs_approval=write_needs_approval
        )
        for tool in tools or []:
            self._registry.add(tool) if isinstance(tool, ToolSpec) else self._registry.function(
                tool
            )

        # call：每个队友就是一个“委派工具”，调用时用队友自己的运行时跑，结果交回。
        # 至于“交出去不回头”的 transfer，属于图编排：同图里 go 到另一个 Agent 节点。
        self.teammates = list(teammates or [])
        for mate in self.teammates:
            self._registry.add(
                ToolSpec(
                    name=mate.name,
                    description=mate.description,
                    func=self._make_delegate(mate),
                    parameters=_TASK_PARAM,
                    side_effect="read",
                )
            )

        self._plan = build_react_plan(
            self._registry, system=instruction, context=context, memory=memory
        )

    @staticmethod
    def _make_delegate(mate: Agent):
        async def delegate(task: str, ctx: Any = None):
            return await mate._run_standalone(task)

        return delegate

    # —— 对内：作为子图/队友/节点时，交出自己编译好的 Plan 与自包含任务 ——
    def add_tool(self, fn: Any, *, side_effect: str = "read", **kw) -> Agent:
        """运行前再补一个工具；side_effect="write" 时执行前会过审批门。"""
        if isinstance(fn, ToolSpec):
            self._registry.add(fn)
        else:
            self._registry.function(fn, side_effect=side_effect, **kw)
        return self

    @property
    def plan(self) -> Plan:
        return self._plan

    def as_task(self):
        """给 Workflow 当节点用：返回一个“跑这个 Agent”的函数体。"""

        async def _task(input: Any, ctx: Any = None):
            return await self._run_standalone(str(input or ""))

        return _task

    def _scheduler(self) -> Scheduler:
        return Scheduler(
            llm=self.model,
            tools=self._registry,
            bus=self.bus,
            store=self.store,
            eventlog=self.eventlog,
        )

    async def _execute(self, task: str, history: list | None = None) -> Any:
        scheduler = self._scheduler()
        run = start_react_run(self._plan, task, history)
        await scheduler.drive(self._plan, run)
        self._last_scheduler = scheduler
        return run

    async def _run_standalone(self, task: str) -> Any:
        """作为别人的子 Agent 时，自包含地跑完并只回传最终产出（call 语义）。"""
        run = await self._execute(task)
        return run.final_output

    # —— 对外主入口 ——
    async def run(self, task: str, *, history: list | None = None) -> AgentResult:
        """跑一轮；传入 history 即在此前对话的基础上继续（多轮由调用方持状态）。"""
        return AgentResult._from(await self._execute(task, history))

    async def resume(self, run_id: str, value: Any = None) -> AgentResult:
        """从一次挂起（如等人审批）中恢复，value 是外部给回的答复。"""
        scheduler = self._scheduler()
        run = await scheduler.resume(self._plan, run_id, value)
        return AgentResult._from(run)

    async def delegate(self, task: str, ctx: Any = None) -> Any:
        """在别的节点/Workflow 里把本 Agent 当子智能体调用（call，要返回，用自己的模型）。"""
        return await self._run_standalone(task)
