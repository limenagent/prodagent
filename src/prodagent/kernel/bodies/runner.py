"""BodyRunner — one dispatch table from body kind to execution.

Everything a body needs from the world arrives injected: governed tool
calls go through the ``ToolExecutor`` (the dispatcher's five-gate
pipeline), pure functions come from the fn table a Workflow populated,
model calls go through the ``LLMInvoker`` the composition root wired, and
autonomous loops through the ``ReactEngine``. The runner imports no
capability package and holds no client — it is the kernel's side of the
port discipline.

Errors propagate: a body that raises is a failed node (the runner's caller
classifies), and a governed suspension (``SuspendPendingApproval``) floats
up untouched so the run parks with the exact call awaiting approval.

Streaming: ``run_stream`` is the primitive — it forwards a body's live
stream events (a ReActBody's Turns) and delivers the result through the
box. ``run`` is the draining form for callers inside a gather, where
nothing streams.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any

from prodagent.kernel.bodies.base import FnBody, LLMBody, ReActBody, SubAgentBody, ToolBody

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping

    from prodagent.kernel.bodies.base import (
        LLMInvoker,
        NodeBody,
        SubagentInvoker,
        ToolExecutor,
    )
    from prodagent.kernel.react import ReactEngine
    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.types import AgentEvent, ToolCall

logger = logging.getLogger(__name__)

__all__ = ["BodyRunner"]


class BodyRunner:
    """Interprets one node body against the injected collaborators."""

    def __init__(
        self,
        tools: ToolExecutor,
        *,
        fns: Mapping[str, Callable[..., Awaitable[Any] | Any]] | None = None,
        llm: LLMInvoker | None = None,
        react: ReactEngine | None = None,
        subagent: SubagentInvoker | None = None,
    ) -> None:
        self._tools = tools
        self._fns = dict(fns) if fns else {}
        self._llm = llm
        self._react = react
        self._subagent = subagent

    async def run(self, body: NodeBody, call: ToolCall, run: AgentRun) -> Any:
        """Draining form: execute one body, no live events. Returns either a
        ``ToolResult`` (governed calls classify themselves) or a raw value
        (fn results, model text) — the node runner coerces whatever arrives
        into the outcome algebra."""
        box: list[Any] = []
        async for _ in self.run_stream(body, call, run, box):
            pass
        return box[0]

    async def run_stream(
        self, body: NodeBody, call: ToolCall, run: AgentRun, box: list[Any]
    ) -> AsyncGenerator[AgentEvent, None]:
        """Streaming form: forwards the body's live stream events while it
        executes (only a ReActBody has any) and appends the result to
        ``box`` — a call-by-reference return for generator control flow."""
        match body:
            case ReActBody():
                if self._react is None:
                    raise RuntimeError(
                        "react node: no ReactEngine wired. Autonomous nodes need the "
                        "Turn loop's collaborators at composition time."
                    )
                if body.goal:
                    # Coarse-planning autonomous node: the goal scopes the
                    # loop, and its finish settles this node, not the run.
                    async for event in self._react.drive(run, goal=body.goal, settle_run=False):
                        yield event
                    box.append(self._react.outcome_of(run, goal_scope=True))
                else:
                    async for event in self._react.drive(run):
                        yield event
                    box.append(self._react.outcome_of(run))
            case ToolBody():
                box.append(await self._tools(call, run_id=run.run_id))
            case FnBody():
                fn = self._fns.get(body.fn)
                if fn is None:
                    raise KeyError(
                        f"fn node {body.fn!r}: no function registered under this name. "
                        "Workflow step functions are registered at compile time — "
                        "was this plan compiled by a different Workflow?"
                    )
                value = fn(**call.params)
                box.append(await value if inspect.isawaitable(value) else value)
            case LLMBody():
                if self._llm is None:
                    raise RuntimeError(
                        "llm node: no LLM invoker wired. LLM bodies need an LLM client "
                        "at composition time (Agent(llm=...) or framework defaults)."
                    )
                # A resolved param may override the declared prompt — that is how
                # upstream output flows into a fixed-prompt step ({{dep.output}}
                # bound to "prompt"), same precedence the old tool wrapper had.
                prompt = str(call.params.get("prompt") or body.prompt)
                box.append(await self._llm(prompt, system=body.system, run_id=run.run_id))
            case SubAgentBody():
                if self._subagent is None:
                    raise RuntimeError(
                        "subagent node: no activation wired. Delegation nodes need "
                        "the activation port at composition time."
                    )
                # The task may flow in from upstream (params resolve {{...}});
                # the declared task is the floor, never the ceiling.
                task = str(call.params.get("task") or body.task or f"Execute {body.agent}")
                result = await self._subagent(body.agent, task, run_id=run.run_id)
                state = str(result.get("state", "failed"))
                if state == "completed":
                    box.append(result)
                elif state == "suspended":
                    from prodagent.kernel.types import ToolResult

                    box.append(
                        ToolResult.suspended(
                            reason=f"child {body.agent!r} awaiting approval",
                            tool="subagent",
                            approval_request_id=str(result.get("approval_request_id", "")),
                        )
                    )
                else:
                    from prodagent.base.errors import ErrorReason
                    from prodagent.kernel.types import ToolError, ToolResult

                    box.append(
                        ToolResult.from_error(
                            ToolError.from_reason(
                                ErrorReason.UNKNOWN,
                                code="subagent_failed",
                                message=str(
                                    result.get("output") or f"child {body.agent!r} ended {state}"
                                ),
                            ),
                            tool="subagent",
                        )
                    )
            case _:
                raise TypeError(f"not a node body: {body!r}")
