"""agent — the user-facing facade: a pure *definition*, hosted at run time.

An Agent holds only a definition — name, model, instruction, tools and
sub_agents — and keeps no ledger of its own. When you run it, the Agent is
hosted by a Scheduler (the runtime), which owns the event log, the state and
artifact stores and the bus. Sub-agents are agents the model can call: when it
does, the sub-agent's plan runs on the *same* Scheduler (one shared ledger, one
trace tree) but with that sub-agent's own model and tools bound to the child Run.

    researcher = Agent("researcher", model=llm,
                       instruction="You are in charge of research", tools=[search])
    result = await researcher.run("Look up X for me")
    print(result.output)

- pass plain functions as tools and the JSON schema is inferred automatically;
- sub_agents are called and return results (agent-as-tool, call semantics);
- a no-return handoff is graph orchestration: in a Workflow treat agents as
  nodes and use go(target_agent, summary) with no edge back — no edge back
  means no return, and no dedicated handoff command is needed;
- context / memory are optional cross-cutting strategies; it runs without them.

One agent calling another is the kernel mechanism recursing on the same
Scheduler: identity (model/tools) belongs to each Agent, the ledger to the host.
See runtime.react, runtime.workflow and Agent.sub_agents for the assembled plans.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.kernel import Plan, Scheduler
from src.runtime.react import build_react_plan, opening
from src.runtime.tools import (
    HardToolError,
    ToolRegistry,
    ToolSpec,
    delegate_to,
)

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


async def spawn_agent(ctx: Any, agent: Agent, task: Any, *, history: list | None = None) -> dict:
    """The raw spawn form: run an Agent's plan on the host Scheduler (shared
    ledger), bound to that Agent's own model/registry, and return the child
    result dict un-translated — for callers that branch on it themselves
    (parallel gathers, hand-written routing). The tool-boundary translation
    (a suspended child parks the caller, a failed child fails the parent) lives
    in delegate_to. The opening state is built here from the task (and any
    prior ``history``) by ReAct's ``opening``; the ledger stays the host
    scheduler's."""
    try:
        return await ctx.spawn(
            agent.plan,
            str(task or ""),
            input=opening(str(task or ""), history),
            llm=agent.model,
            tools=agent.registry,
        )
    except RecursionError as exc:
        # The kernel's depth guard speaks RecursionError; across the tool boundary
        # this must be HardToolError so it propagates structurally (never becomes
        # feedback the model could "correct"). The precise industry reference is
        # ADK's task-delegation path, where a failed child fails its parent, and
        # whose NodeInterruptedError is deliberately a BaseException so
        # `except Exception` cannot swallow it — the same carve-out. ADK's older
        # agent-tool paths instead feed the error string back to the model, and
        # it has no recursion guard at that boundary at all.
        raise HardToolError(str(exc)) from exc


def _sub_agent_tool(agent: Agent):
    """Build the tool function for one sub-agent (call semantics)."""

    async def call(task: str, ctx: Any) -> Any:
        # delegate_to carries the two-step resume and terminal-state translation
        # (suspended -> DelegationSuspendedError, failed -> HardToolError); the
        # opening message and this Agent's own model/registry are bound here.
        return await delegate_to(
            ctx,
            agent.plan,
            task,
            input=opening(task),
            llm=agent.model,
            tools=agent.registry,
        )

    return call


class Agent:
    def __init__(
        self,
        name: str,
        *,
        model: Any = None,
        instruction: str = "",
        description: str = "",
        tools: list | None = None,
        sub_agents: list[Agent] | None = None,
        context: Any = None,
        memory: Any = None,
        registry: ToolRegistry | None = None,
        write_needs_approval: bool = True,
    ):
        self.name = name
        self.model = model
        self.instruction = instruction
        # description is the capability blurb a "supervisor model" reads when
        # picking a sub-agent; defaults to the first line of the instruction.
        self.description = description or (instruction.splitlines()[0] if instruction else name)
        self.context = context
        self.memory = memory

        # Accept a pre-built registry (e.g. one with MCP tools); otherwise build one.
        self._registry = registry or ToolRegistry(write_needs_approval=write_needs_approval)
        for tool in tools or []:
            if isinstance(tool, ToolSpec):
                self._registry.add(tool)
            else:
                self._registry.function(tool)

        # Each sub-agent becomes a "delegation tool": its plan runs on the host
        # Scheduler when the model calls it (see _sub_agent_tool).
        self.sub_agents: list[Agent] = []
        for sub in sub_agents or []:
            self.add_sub_agent(sub)

        # The agent's name is its blueprint's name: every Run of this plan
        # derives it, and run_started carries it for observers.
        self._plan = build_react_plan(
            self._registry, name=self.name, system=instruction, context=context, memory=memory
        )
        self._runtime: Scheduler | None = None  # bound when the tree is hosted

    # ---- inward: when used as a subgraph/sub-agent/node, hand out its compiled Plan ----
    @property
    def plan(self) -> Plan:
        return self._plan

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    def add_tool(self, fn: Any, *, side_effect: str = "read", **kw) -> Agent:
        """Add one more tool before running; with side_effect="write" it passes an approval gate first."""
        if isinstance(fn, ToolSpec):
            self._registry.add(fn)
        else:
            self._registry.function(fn, side_effect=side_effect, **kw)
        return self

    def add_sub_agent(self, agent: Agent) -> Agent:
        """Register a sub-agent as a delegation tool (call semantics). Use this to
        wire agents that reference each other — a constructor cannot pass a cycle."""
        self.sub_agents.append(agent)
        self._registry.add(
            ToolSpec(
                name=agent.name,
                description=agent.description,
                func=_sub_agent_tool(agent),
                parameters=_TASK_PARAM,
                side_effect="read",
                delegation=True,
            )
        )
        return self

    # ---- hosting: bind this whole agent tree to one shared Scheduler ----
    def _bind(self, scheduler: Scheduler, _seen: set | None = None) -> None:
        _seen = _seen if _seen is not None else set()
        if id(self) in _seen:
            return
        _seen.add(id(self))
        self._runtime = scheduler
        # point the registry's write gate at the host scheduler's bus
        self._registry.attach_bus(scheduler.bus)
        for sub in self.sub_agents:
            sub._bind(scheduler, _seen)

    def host(self, **kw) -> Scheduler:
        """Create (and bind onto) the runtime Scheduler that owns the ledger.

        Use this instead of run() when you must observe the live event stream
        (``scheduler.bus``) or inject durable stores before running — then drive
        it yourself: ``await scheduler.run(agent.plan, task=...)``. The Agent and
        all sub-agents bind their identity (model/tools) onto that one Scheduler.
        Repeated ``host()`` with no injections returns the same Scheduler, so a
        run and a later turn/resume share the one ledger.
        """
        if self._runtime is not None and not kw:
            return self._runtime
        scheduler = Scheduler(llm=self.model, tools=self._registry, **kw)
        self._bind(scheduler)
        return scheduler

    # ---- main outward entry points ----
    async def run(self, task: str, *, history: list | None = None) -> AgentResult:
        """Run one turn; pass history to continue a prior conversation (caller holds multi-turn state)."""
        scheduler = self.host()
        run = await scheduler.run(self._plan, task=task, input=opening(task, history))
        return AgentResult._from(run)

    async def resume(self, run_id: str, value: Any = None) -> AgentResult:
        """Resume from a suspension (e.g. awaiting approval); value is the external reply."""
        scheduler = self._runtime or self.host()
        run = await scheduler.resume(
            self._plan, run_id, value, llm=self.model, tools=self._registry
        )
        return AgentResult._from(run)
