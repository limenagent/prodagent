"""tools — unified tool layer: local functions, MCP tools, and sub-agents all converge here.

To the model there is only one kind of "tool": a name, a description written for
the model, a parameter schema, and the function that actually runs. Whether it
comes from a local Python function, an MCP server, or "calling another agent",
it is flattened at this layer into the same ToolSpec and runs the same pipeline:

    existence check -> argument validation -> write ops pass an approval gate
    (bus.check) -> execute -> uniform ToolResult

It satisfies the kernel's ToolPort: the Scheduler only knows dispatch(call) and
doesn't care where the tool came from.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.kernel import ToolCall, ToolResult

_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


class HardToolError(Exception):
    """Raised by delegation infrastructure (depth guard, failed child Run).

    The single carve-out from "tool exceptions become feedback": these must
    reach flow control — fail the Run (and propagate up the tree), or park it
    (DelegationSuspendedError). A flaky teammate recovers through node-level
    retry, not through the model reading an error string.
    """


class DelegationSuspendedError(HardToolError):
    """A delegated child Run parked; the caller's flow must park too.

    Tool-shaped delegation cannot return an Outcome, so the suspension rides
    the HardToolError carve-out (never feedback) and the recipe's tool loop
    turns it back into Outcome.park. Carries what the parent's park needs.
    """

    def __init__(self, child_run_id: str, question: str = "", task: str = ""):
        super().__init__(question or "delegated child is suspended")
        self.child_run_id = child_run_id
        self.question = question
        self.task = task


def infer_schema(fn: Callable) -> dict:
    """Infer a minimal JSON Schema from signature and type hints (teaching build, no third party)."""
    sig = inspect.signature(fn)
    properties, required = {}, []
    # Convention: ctx is framework-injected context, not a parameter the model
    # fills in, so skip it.
    for name, p in sig.parameters.items():
        if name in ("ctx", "_ctx", "context"):
            continue
        json_type = _PY_TO_JSON.get(p.annotation, "string")
        properties[name] = {"type": json_type}
        if p.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "object", "properties": properties, "required": required}


@dataclass
class ToolSpec:
    name: str
    description: str
    func: Callable
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    # read = read-only, safe to parallelize/retry; write = has side effects,
    # passes an approval gate before execution.
    side_effect: str = "read"
    # calling this tool births a child Run: the two-step-resume short-circuit
    # applies to it, and a suspension in a multi-delegation turn is refused.
    delegation: bool = False


class ToolRegistry:
    """Tool registry plus a governed execution pipeline (it is itself a ToolPort)."""

    def __init__(self, *, bus: Any = None, write_needs_approval: bool = True):
        self._tools: dict[str, ToolSpec] = {}
        self.bus = bus
        self.write_needs_approval = write_needs_approval

    def attach_bus(self, bus: Any) -> None:
        """Point the approval gate at a (shared) bus after construction."""
        self.bus = bus

    def is_delegation(self, name: str) -> bool:
        """Does calling this tool birth a child Run (the resume short-circuit applies)?"""
        spec = self._tools.get(name)
        return bool(spec and spec.delegation)

    # ---- registration ----
    def add(self, spec: ToolSpec) -> ToolRegistry:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool name: {spec.name}")
        self._tools[spec.name] = spec
        return self

    def function(
        self,
        fn: Callable,
        *,
        name: str | None = None,
        description: str = "",
        side_effect: str = "read",
    ) -> ToolRegistry:
        """Register an ordinary Python function as a tool; schema is inferred from its signature."""
        doc = (inspect.getdoc(fn) or "").strip()
        desc = description or (doc.splitlines()[0] if doc else "")
        spec = ToolSpec(
            name=name or fn.__name__,
            description=desc,
            func=fn,
            parameters=infer_schema(fn),
            side_effect=side_effect,
        )
        return self.add(spec)

    def schemas(self) -> list[dict]:
        """The function-calling tool list shown to the model."""
        return [
            {
                "type": "function",
                "function": {
                    "name": s.name,
                    "description": s.description,
                    "parameters": s.parameters,
                },
            }
            for s in self._tools.values()
        ]

    # ---- governed execution ----
    async def dispatch(self, call: ToolCall, ctx: Any = None) -> ToolResult:
        spec = self._tools.get(call.name)
        if spec is None:
            return ToolResult.failure(f"no such tool: {call.name}", call.call_id)

        missing = [k for k in spec.parameters.get("required", []) if k not in call.arguments]
        if missing:
            # A bad argument is "feedback", not a crash: tell the model what's
            # missing so it can fix it next round.
            return ToolResult.failure(f"missing required arguments: {missing}", call.call_id)

        if spec.side_effect == "write" and self.write_needs_approval and self.bus is not None:
            verdict = await self.bus.check(f"tool:{spec.name}", call=call, ctx=ctx)
            if not verdict.allowed:
                return ToolResult.failure(f"operation not approved: {verdict.reason}", call.call_id)

        try:
            result = self._invoke(spec.func, call.arguments, ctx)
            if inspect.isawaitable(result):
                result = await result
            return ToolResult.success(result, call.call_id)
        except HardToolError:
            # The one exception to "never blow up the graph": delegation
            # infrastructure signals must reach flow control (fail or park),
            # not become feedback.
            raise
        except Exception as exc:  # tool exceptions also become feedback, never blow up the graph
            return ToolResult.failure(f"{type(exc).__name__}: {exc}", call.call_id)

    @staticmethod
    def _invoke(fn: Callable, arguments: dict, ctx: Any) -> Any:
        # Support both calling conventions:
        # - ordinary business function: parameters are the fields the model
        #   fills (weather(city, ctx)), injected by name;
        # - adapter-style function (e.g. an MCP caller): a single
        #   arguments/args/payload parameter receives the whole dict.
        # A parameter named ctx is filled by the framework, not the model.
        sig = inspect.signature(fn)
        ctx_name = next((n for n in sig.parameters if n in ("ctx", "_ctx", "context")), None)
        kwargs: dict[str, Any] = {ctx_name: ctx} if ctx_name else {}
        data_params = [n for n in sig.parameters if n != ctx_name]
        if len(data_params) == 1 and data_params[0] in ("arguments", "args", "payload"):
            kwargs[data_params[0]] = arguments
        else:
            for name, p in sig.parameters.items():
                if name == ctx_name:
                    continue
                if name in arguments:
                    kwargs[name] = arguments[name]
                elif p.default is inspect.Parameter.empty:
                    raise TypeError(f"missing argument: {name}")
                else:
                    kwargs[name] = p.default
        return fn(**kwargs)
