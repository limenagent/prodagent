"""LoopBody — the think-act loop as a node body (column 3/23's recipe half).

The kernel has five body kinds and "autonomous loop" is deliberately not
one of them (column 3: ReAct is a strategy, not a mechanism). This module
is where the loop lives instead: a declarative, wire-friendly body whose
execution is a whole think-act loop, driven by a :class:`LoopDriver`
(the runtime's AgentLoop implements it) that arrives per-execution through
the NodeContext's generic ``wiring`` bag — ``ctx.wiring["loop_driver"]``.

``drives_run = True`` is the body's one contract with the engine beyond
NodeBody: a body that drives the whole run commits the run's transcript
itself (the node completes with the run's output, unwrapped — no tool
fragment), and its crash is the *run's* crash, never node data to replan
around. The kernel reads that flag with no idea what a "loop" is.

With a ``goal`` this is the coarse-planning shape (column 7): the planner
declared WHAT to achieve; the loop works out HOW with the same tools, and
its finish settles the node, not the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from prodagent.kernel.body import NodeContext, Outcome

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from prodagent.kernel.run import Run
    from prodagent.kernel.types import AgentEvent, ToolCall, ToolResult

__all__ = ["LoopBody", "LoopDriver", "LOOP_DRIVER_KEY"]

LOOP_DRIVER_KEY = "loop_driver"
"""The wiring-bag key a composition root registers the loop driver under.
A string convention between recipes and the composition root — the kernel
never reads it."""


@runtime_checkable
class LoopDriver(Protocol):
    """What a LoopBody drives — the runtime's loop machinery, as the recipe
    sees it. ``drive`` iterates the loop over a run (streaming its live
    events); ``outcome_of`` folds the run's terminal flag into node-outcome
    data. The implementation lives above the kernel (runtime/agent_loop);
    the kernel sees only the wiring bag it travels in."""

    def drive(self, run: Run, *, goal: str | None = None, settle_run: bool = True) -> Any: ...

    def outcome_of(self, run: Run, *, goal_scope: bool = False) -> ToolResult: ...


@dataclass(frozen=True)
class LoopBody:
    """L3 — a think-act loop as one node's body: rounds of model calls and
    tool batches until the model says done. The loop lives *inside* the
    body; from the scheduler's view this is still just one node."""

    kind = "loop"
    goal: str = ""
    readonly = False
    drives_run = True

    @property
    def target(self) -> str:
        return "loop"

    def _driver(self, ctx: NodeContext) -> LoopDriver:
        driver = ctx.wiring.get(LOOP_DRIVER_KEY)
        if driver is None:
            raise RuntimeError(
                "loop node: no loop driver on this context's wiring. The "
                "composition root registers the loop machinery under "
                f"{LOOP_DRIVER_KEY!r} — a loop body without its driver is a "
                "composition bug, not a runtime condition."
            )
        return driver  # type: ignore[no-any-return]

    def run_stream(
        self, input: ToolCall, ctx: NodeContext, box: list[Outcome]
    ) -> AsyncGenerator[AgentEvent, None]:
        """The loop's native form: rounds as they happen, one Outcome boxed."""
        driver = self._driver(ctx)
        if ctx.run is None:
            raise RuntimeError("loop node: no live run on this context to drive.")
        return self._drive(driver, ctx, input, box)

    async def _drive(
        self, driver: LoopDriver, ctx: NodeContext, input: ToolCall, box: list[Outcome]
    ) -> AsyncGenerator[AgentEvent, None]:
        assert ctx.run is not None  # guarded by run_stream
        # A resolved param may override the declared goal — that is how an
        # upstream planner node's output scopes this loop ({{planner.output}}
        # bound to "goal"): plan-and-resolve as two nodes and an edge.
        goal = str(input.params.get("goal") or self.goal)
        if goal:
            # Coarse-planning shape: the goal scopes the loop, and its
            # finish settles this node, not the run.
            async for event in driver.drive(ctx.run, goal=goal, settle_run=False):
                yield event
            box.append(Outcome(value=driver.outcome_of(ctx.run, goal_scope=True)))
            return
        async for event in driver.drive(ctx.run):
            yield event
        box.append(Outcome(value=driver.outcome_of(ctx.run)))

    async def run(self, input: ToolCall, ctx: NodeContext) -> Outcome:
        """Draining form: same execution, events dropped on the floor."""
        box: list[Outcome] = []
        async for _ in self.run_stream(input, ctx, box):
            pass
        return box[0]
