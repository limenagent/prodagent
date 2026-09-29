"""workflow — declarative orchestration facade for users.

Use Workflow when you don't want "an agent that thinks on its own" but "a flow
chart you can see clearly":

    wf = Workflow()
    wf.add_node("fetch", fetch_fn)
    wf.add_node("write", writer_agent)   # a node can also be an Agent directly
    wf.add_edge("fetch", "write")
    wf.entry("fetch")
    result = await wf.run("task")

A node function is just `async def fn(input, ctx)` with permissive returns: a
bare value is passed downstream, a dict writes shared state. To steer control
flow use this module's go / send / wait_human (go to another Agent node without
a return edge is a "hand off and don't come back" transfer; return a list of
sends to fan out parallel copies). State keys not declared up front get an
automatic last channel, so you needn't learn reducers to get started.

A Workflow is a pure definition: like an Agent it keeps no ledger. When run it
is hosted by a Scheduler that owns the event log, stores and bus; an Agent node
runs on that same Scheduler (shared ledger, one trace tree) with its own
model/tools. Underneath it is still the Plan/Node/Scheduler engine; this layer
only makes declaration more ergonomic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.kernel import (
    FnBody,
    Node,
    NodeBody,
    Outcome,
    Plan,
    Run,
    Scheduler,
    SubPlanBody,
    last,
)
from src.runtime.agent import Agent
from src.runtime.react import opening
from src.runtime.tools import DelegationSuspendedError

# ---- Convenience helpers for control flow inside a node (no need to import
# the kernel's Outcome/Command). ----


def go(target: str, value: Any = None, **state_delta) -> Outcome:
    """Transition to a node (back-edges, loops, and handoffs all use it); value is this activation's input."""
    return Outcome.goto(target, value, **state_delta)


def send(template: str, payload: Any, key: str | None = None) -> Outcome:
    """Instantiate one copy of a template node at runtime and feed it payload (the kernel's Send).

    To fan out several parallel copies at once, return a list:
    return [send("w", x) for x in items]. It's fine that the count is unknown
    until runtime; the engine runs them concurrently in the same wave.
    """
    return Outcome.send(template, payload, key)


def wait_human(question: str = "", payload: Any = None, *, kind: str = "approval") -> Outcome:
    """Truly stop here and wait for external input (approval / more info), then continue with wf.resume."""
    return Outcome.park(kind, payload, question)


class _FacadeBody:
    """Wrap a user function: normalize its permissive return into an Outcome and auto-add channels for new state keys."""

    def __init__(self, fn: Callable, plan_ref: list):
        self._inner = FnBody(fn)
        self.plan_ref = plan_ref

    async def run(self, input: Any, ctx) -> Outcome:
        try:
            outcome = await self._inner.run(input, ctx)
        except DelegationSuspendedError as exc:
            # a delegated Agent parked: lift the suspension into this graph
            # too (same law as the ReAct tool turn), instead of failing the node
            return Outcome.park(
                "delegation",
                payload={"child_run_id": exc.child_run_id, "task": exc.task},
                question=exc.question,
            )
        plan = self.plan_ref[0]
        for k in outcome.state_delta:  # auto-add a last channel for undeclared keys
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


class Workflow:
    def __init__(
        self, *, model: Any = None, tools: Any = None, max_waves: int = 64, concurrency: int = 8
    ):
        self._model = model
        self._tools = tools
        self.max_waves = max_waves
        self.concurrency = concurrency

        self._nodes: dict[str, tuple[Any, dict]] = {}
        self._edges: list[tuple[str, str, Any]] = []
        self._entry: list[str] = []
        self._channels: dict[str, Any] = {}

        # cached compile/host, shared by run and resume (reset if you edit the graph)
        self._compiled: Plan | None = None
        self._runtime: Scheduler | None = None

    # ---- declaration ----
    def _touch(self) -> None:
        # the graph changed: a previously compiled Plan is stale and must not be
        # reused by a later run — hosting early can never freeze a half-built graph
        self._compiled = None

    def channel(self, name: str, reducer: Any) -> Workflow:
        """Explicitly declare a state channel and its merge rule (e.g. append/add/merge)."""
        self._channels[name] = reducer
        self._touch()
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
        """Declare a node; the body may be a function, an Agent, a bare Plan, or a kernel body."""
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
        self._touch()
        return self

    def add_edge(self, src: str, dst: str, *, when: Callable | None = None) -> Workflow:
        self._edges.append((src, dst, when))
        self._touch()
        return self

    def branch(self, src: str, routes: dict[str, str], *, decide: Callable) -> Workflow:
        """Conditional branch: decide(state) returns a key of routes, and the edge is chosen accordingly."""
        for key, dst in routes.items():
            self._edges.append((src, dst, lambda s, k=key: decide(s) == k))
        self._touch()
        return self

    def entry(self, *names: str) -> Workflow:
        self._touch()
        self._entry = list(names)
        return self

    # ---- compile ----
    def _as_body(self, body: Any, plan_ref: list) -> NodeBody:
        if isinstance(body, Agent):
            # An Agent node runs on the host Scheduler (shared ledger, one trace
            # tree), bound to that Agent's own model and registry.
            # seed=opening maps the node's task to the Agent's opening user
            # message; the child still runs on the shared ledger (one trace tree).
            return SubPlanBody(body.plan, llm=body.model, tools=body.registry, seed=opening)
        if isinstance(body, Plan):  # a bare Plan is recursed by the same scheduler
            return SubPlanBody(body)
        if callable(body):  # ordinary function -> permissive wrapper
            return _FacadeBody(body, plan_ref)
        return body  # already a kernel body, use as-is

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

    def host(
        self,
        *,
        bus: Any = None,
        store: Any = None,
        eventlog: Any = None,
        blobs: Any = None,
    ) -> Scheduler:
        """Create (and bind onto) the runtime Scheduler that owns the ledger.

        Use this instead of run() to observe the live stream (``host(bus=...)``)
        or inject durable stores; a following run/resume reuses it. Agent nodes
        bind onto it (their registries point at its bus) but keep their identity.
        """
        if self._runtime is not None and not any(
            x is not None for x in (bus, store, eventlog, blobs)
        ):
            return self._runtime  # already hosted; run/resume share the one ledger
        scheduler = Scheduler(
            llm=self._model,
            tools=self._tools,
            bus=bus,
            store=store,
            eventlog=eventlog,
            blobs=blobs,
            max_waves=self.max_waves,
            concurrency=self.concurrency,
        )
        for body, _ in self._nodes.values():
            if isinstance(body, Agent):
                body._bind(scheduler)
        self._runtime = scheduler
        return scheduler

    # ---- run ----
    def _ensure(self) -> tuple[Plan, Scheduler]:
        # Compile and host once; run and resume share the same plan and ledger.
        if self._compiled is None:
            self._compiled = self._compile()
        if self._runtime is None:
            self.host()
        return self._compiled, self._runtime

    async def run(self, input: Any = None) -> WorkflowResult:
        plan, scheduler = self._ensure()
        run = Run.start(plan, task="", llm=self._model, tools=self._tools)
        if isinstance(input, dict):  # a dict seeds initial shared state
            for k, v in input.items():
                plan.channels.setdefault(k, last(None))
                run.shared[k] = v
        elif isinstance(input, str):
            run.task = input
        await scheduler.drive(plan, run)
        return WorkflowResult._from(run)

    async def resume(self, run_id: str, value: Any = None) -> WorkflowResult:
        plan, scheduler = self._ensure()
        run = await scheduler.resume(plan, run_id, value, llm=self._model, tools=self._tools)
        return WorkflowResult._from(run)
