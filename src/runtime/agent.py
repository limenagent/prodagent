"""agent — the user-facing "agent" facade (mechanism inside, ergonomics outside).

The kernel only knows parts like Plan/Node/Scheduler; in daily use you'd rather
face "an Agent with a name, a model, tools, that can delegate to teammates and
hand work off". This facade assembles those parts in the most common way:

    researcher = Agent(name="researcher", model=llm,
                       instruction="You are in charge of research", tools=[search])
    result = await researcher.run("Look up X for me")
    print(result.output)

Key positioning: **each Agent carries its own model, tools, and runtime, and is a
self-contained execution unit.**
- pass plain functions as tools and the schema is inferred automatically;
- teammates are sub-agents you delegate to and that return results (call /
  agent-as-tool);
- a no-return transfer is graph orchestration: in a Workflow treat agents as
  nodes and use go(target_agent, handoff_summary) with no return edge — no edge
  back means no return, and no dedicated handoff command is needed;
- context / memory are optional cross-cutting strategies; it runs without them.

One agent calling another is, at bottom, one of its node bodies running the same
kernel again — what recurses is the kernel mechanism itself, with no requirement
to share one Scheduler, so each agent uses its own model without cross-talk. To
see how the parts fit, return to runtime.react / runtime.multiagent.
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
    "properties": {"task": {"type": "string", "description": "The task to hand to this sub-agent"}},
    "required": ["task"],
}


@dataclass
class AgentResult:
    """The result of one agent run; output is the final user-facing answer, the rest aids debugging."""

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
        # description is the capability blurb a "supervisor model" reads when
        # picking a subordinate; defaults to the first line of the instruction.
        self.description = description or (instruction.splitlines()[0] if instruction else name)
        self.context = context
        self.memory = memory
        self.bus = bus or Bus()
        self.store = store or InMemoryStore()
        self.eventlog = eventlog or InMemoryEventLog()

        # Accept a pre-built registry (e.g. one with MCP tools attached); otherwise build one.
        self._registry = registry or ToolRegistry(
            bus=self.bus, write_needs_approval=write_needs_approval
        )
        for tool in tools or []:
            self._registry.add(tool) if isinstance(tool, ToolSpec) else self._registry.function(
                tool
            )

        # call: each teammate is a "delegation tool", run on the teammate's own
        # runtime when called and returning its result. As for the no-return
        # transfer, that is graph orchestration: go to another Agent node in the same graph.
        self.teammates = list(teammates or [])
        for mate in self.teammates:
            # Assembly, not mechanism: the whole delegation tree (any depth)
            # shares this bus, so events and approval gates land on one
            # observable stream even when nobody passed a bus explicitly.
            mate.share_bus(self.bus)
            self._registry.add(
                ToolSpec(
                    name=mate.name,
                    description=mate.description,
                    func=self._make_delegate(mate),
                    parameters=_TASK_PARAM,
                    side_effect="read",
                )
            )

        # The agent's name is its blueprint's name: every Run of this plan
        # derives it, and run_started carries it for observers.
        self._plan = build_react_plan(
            self._registry, name=self.name, system=instruction, context=context, memory=memory
        )

    @staticmethod
    def _make_delegate(mate: Agent):
        async def delegate(task: str, ctx: Any = None):
            return await mate._run_standalone(task)

        return delegate

    # ---- inward: when used as a subgraph/teammate/node, hand out its compiled Plan and self-contained task ----
    def add_tool(self, fn: Any, *, side_effect: str = "read", **kw) -> Agent:
        """Add one more tool before running; with side_effect="write" it passes an approval gate first."""
        if isinstance(fn, ToolSpec):
            self._registry.add(fn)
        else:
            self._registry.function(fn, side_effect=side_effect, **kw)
        return self

    @property
    def plan(self) -> Plan:
        return self._plan

    def as_task(self):
        """For use as a Workflow node: return a function body that runs this Agent."""

        async def _task(input: Any, ctx: Any = None):
            return await self._run_standalone(str(input or ""))

        return _task

    def share_bus(self, bus: Any, _seen: set | None = None) -> None:
        """Assembly-time wiring: point this agent and every teammate,
        recursively, at one bus — a whole delegation tree lands on one
        observable stream (and one approval gate). The _seen guard keeps
        mutual-teammate cycles from recursing forever."""
        _seen = _seen if _seen is not None else set()
        if id(self) in _seen:
            return
        _seen.add(id(self))
        self.bus = bus
        self._registry.attach_bus(bus)
        for mate in self.teammates:
            mate.share_bus(bus, _seen)

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
        return run

    async def _run_standalone(self, task: str) -> Any:
        """When acting as someone's sub-agent, run self-contained and return only final output (call semantics)."""
        run = await self._execute(task)
        return run.final_output

    # ---- main outward entry point ----
    async def run(self, task: str, *, history: list | None = None) -> AgentResult:
        """Run one turn; pass history to continue a prior conversation (caller holds multi-turn state)."""
        return AgentResult._from(await self._execute(task, history))

    async def resume(self, run_id: str, value: Any = None) -> AgentResult:
        """Resume from a suspension (e.g. awaiting approval); value is the external reply."""
        scheduler = self._scheduler()
        run = await scheduler.resume(self._plan, run_id, value)
        return AgentResult._from(run)

    async def delegate(self, task: str, ctx: Any = None) -> Any:
        """Call this Agent as a sub-agent from another node/Workflow (call, returns, uses its own model)."""
        return await self._run_standalone(task)
