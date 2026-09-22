"""body — the single composable interface, plus four built-in bodies.

In the kernel's eyes there is only one kind of "schedulable thing": an
executable that satisfies the NodeBody protocol, takes input and a
NodeContext, and returns an Outcome. So:

- a plain function (FnBody) is a body;
- one governed tool call (ToolBody) is a body;
- one fixed-prompt model call (LLMBody) is a body;
- activating a sub-agent / sub-plan (SubPlanBody) is also a body.

There is no separate "macro node vs micro agent" vocabulary, only bodies
nested inside bodies — multi-agent is simply some body recursively running
another graph.

An Outcome is what a body produces, split orthogonally into:
- value: a value for downstream nodes;
- state_delta: data folded into shared state via reducers;
- control: a Goto/Send command (None = follow static edges naturally);
- suspend: when set, request to be released and suspended at this point.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from src.kernel.command import Command, Goto, Send
from src.kernel.run import Interrupt
from src.kernel.types import ToolCall


@dataclass(frozen=True)
class Outcome:
    value: Any = None
    state_delta: dict[str, Any] = field(default_factory=dict)
    # control can be one command or a group of them (fan out several Sends).
    control: Command | list[Command] | None = None
    suspend: Interrupt | None = None

    # — convenient constructors that read like "stating intent" —
    @classmethod
    def ok(cls, value: Any = None, **delta: Any) -> Outcome:
        return cls(value=value, state_delta=dict(delta))

    @classmethod
    def goto(
        cls, target: str, payload: Any = None, *, immediate: bool = True, **delta: Any
    ) -> Outcome:
        """Transition to target; payload is its next input, delta folds into state."""
        return cls(state_delta=dict(delta), control=Goto(target, immediate, payload))

    @classmethod
    def send(cls, template: str, payload: Any, key: str | None = None) -> Outcome:
        return cls(control=Send(template, payload, key))

    @classmethod
    def fan_out(cls, *sends: Send, **delta: Any) -> Outcome:
        """Dynamically fan out to several instances at once; delta may also
        write shared state (e.g. the step list)."""
        return cls(state_delta=dict(delta), control=list(sends))

    @classmethod
    def park(cls, kind: str, payload: Any = None, question: str = "") -> Outcome:
        return cls(suspend=Interrupt(kind, payload, question))


def coerce_outcome(raw: Any) -> Outcome:
    """Let a body be terse: a bare value/dict/command/group is normalized into
    an Outcome automatically."""
    if raw is None:
        return Outcome()
    if isinstance(raw, Outcome):
        return raw
    if isinstance(raw, Command):
        return Outcome(control=raw)
    if isinstance(raw, list):
        # Treat it as a control group only when the whole group is control
        # intent (commands/Outcomes), e.g. fanning out several Sends. An ordinary
        # list (such as sorted results) stays a business value — being a list
        # alone must not make us guess it is a command, or returning lists in
        # business logic would be misread.
        if raw and all(isinstance(x, (Command, Outcome)) for x in raw):
            controls: list = []
            delta: dict = {}
            for item in raw:
                oc = coerce_outcome(item)
                if oc.control is not None:
                    controls.extend(oc.control if isinstance(oc.control, list) else [oc.control])
                delta.update(oc.state_delta)
            return Outcome(state_delta=delta, control=controls or None)
        return Outcome(value=raw)
    if isinstance(raw, dict):
        # A bare dict folds into state; return Outcome.ok(d) when the dict itself is the value.
        return Outcome(state_delta=raw)
    return Outcome(value=raw)


@runtime_checkable
class NodeBody(Protocol):
    async def run(self, input: Any, ctx: NodeContext) -> Outcome: ...


class NodeContext:
    """The "service wiring" a body can use while running.

    Note this is wiring, not data: it holds live objects like the model port
    and tool port, so it is never serialized. A run's data travels via
    input / state_delta and is never smuggled into context — that boundary keeps
    checkpoints clean.
    """

    def __init__(
        self,
        run: Any,
        node_id: str,
        *,
        llm: Any = None,
        tools: Any = None,
        subagent: Any = None,
        bus: Any = None,
        resume_value: Any = None,
    ):
        self.run = run
        self.node_id = node_id
        self._llm = llm
        self._tools = tools
        self._subagent = subagent
        self._bus = bus
        self.resume_value = resume_value

    @property
    def shared(self) -> dict[str, Any]:
        """Read-only view of shared state; to change state return state_delta and
        let the engine fold it at the barrier."""
        return self.run.shared

    @property
    def run_id(self) -> str:
        return self.run.run_id

    async def emit(self, event: str, **data: Any) -> None:
        if self._bus is not None:
            await self._bus.fire(event, run_id=self.run_id, node_id=self.node_id, **data)

    async def llm_complete(self, prompt: str, system: str | None = None) -> str:
        """One fixed-prompt model call: it processes input, it doesn't decide flow."""
        reply = await self.llm_chat([{"role": "user", "content": prompt}], system=system)
        return reply.text

    async def llm_chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        system: str | None = None,
        on_delta: Any = None,
    ):
        """Make one chat call through the model port and meter it uniformly
        (returns the normalized LlmReply).

        on_delta is passed through to the implementation for token streaming;
        metering and normalization still happen here.
        """
        if self._llm is None:
            raise RuntimeError("no LlmPort injected; cannot call the model")
        reply = await self._llm.chat(messages, tools=tools, system=system, on_delta=on_delta)
        self.run.metrics["llm_calls"] += 1
        self.run.metrics["tokens"] += reply.tokens
        return reply

    async def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """Make one governed call through the tool port, returning a ToolResult."""
        if self._tools is None:
            raise RuntimeError("no ToolPort injected; cannot call a tool")
        self.run.metrics["tool_calls"] += 1
        # unique per call (the Run's counter); not retry/park-stable — see ToolCall
        count = self.run.metrics["tool_calls"]
        call = ToolCall(name, arguments or {}, call_id=f"{self.run_id}:{self.node_id}:{count}")
        return await self._tools.dispatch(call, ctx=self)

    async def spawn(self, spec: Any, task: str, payload: Any = None) -> dict:
        """Activate a child Run (call semantics: it returns its result when done)."""
        if self._subagent is None:
            raise RuntimeError("no SubagentPort injected; cannot activate a sub-agent")
        return await self._subagent.activate(spec, task, self.run, payload, node_id=self.node_id)


# ════════════ Four built-in bodies ════════════


class FnBody:
    """L0: a Python function (plain or async).

    The function may be written as f(x) taking only input, or f(x, ctx) to use
    services; the kernel adapts by the number of parameters.
    """

    def __init__(self, fn: Any):
        self.fn = fn
        self._params = len(inspect.signature(fn).parameters)

    async def run(self, input: Any, ctx: NodeContext) -> Outcome:
        args = (input, ctx) if self._params >= 2 else (input,)
        result = self.fn(*args)
        if inspect.isawaitable(result):
            result = await result
        return coerce_outcome(result)


class ToolBody:
    """L1: make one governed tool call by name; input is the arguments."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name

    async def run(self, input: Any, ctx: NodeContext) -> Outcome:
        result = await ctx.call_tool(self.tool_name, input if isinstance(input, dict) else {})
        # Tool success/failure comes back as value; the upstream/model decides the
        # next step rather than the engine dying on a raise.
        return Outcome.ok(result)


class LLMBody:
    """L2: one fixed-prompt model call. prompt may be a string or an
    input->str function."""

    def __init__(self, prompt: Any, system: str | None = None):
        self.prompt = prompt
        self.system = system

    async def run(self, input: Any, ctx: NodeContext) -> Outcome:
        text = self.prompt if isinstance(self.prompt, str) else self.prompt(input)
        answer = await ctx.llm_complete(text, system=self.system)
        return Outcome.ok(answer)


class SubPlanBody:
    """L3: activate a sub-plan/sub-agent, run it recursively with the same
    kernel, and fold its terminal state."""

    def __init__(self, spec: Any):
        self.spec = spec

    async def run(self, input: Any, ctx: NodeContext) -> Outcome:
        if ctx.resume_value is not None:
            # two-step resume: the child was resumed separately and its output
            # arrived as this node's resume value; re-spawning here would orphan
            # the child that already did the work. (A child that legitimately
            # completes with None output needs a sentinel value from the
            # operator: None is indistinguishable from "not resumed".)
            return Outcome.ok(ctx.resume_value)
        result = await ctx.spawn(self.spec, str(input or ""))
        if result.get("state") == "suspended":
            # a parked child parks the caller too: the question travels up and
            # the payload carries the child's run_id so resume can find it
            return Outcome.park(
                "delegation",
                payload={"child_run_id": result["run_id"], "task": str(input or "")},
                question=result.get("question", ""),
            )
        # Call semantics: by default return only the child Run's final output; a
        # custom body can pull the full result.
        return Outcome.ok(result.get("output"))
