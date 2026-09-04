"""tools —— 统一工具层：本地函数、MCP 工具、子 Agent 都归一到这里（第 14、15、17 课）。

对模型而言，世界上只有一种“工具”：一个名字、一段给模型看的说明、一份参数
schema，以及背后真正执行的函数。无论它来自本地 Python 函数、MCP Server，还是
“调用另一个 Agent”，都在这一层拉平成同样的 ToolSpec，走同一条调度管线：

    存在性校验 → 参数校验 → 写操作过审批门(bus.check) → 执行 → 统一 ToolResult

它满足内核的 ToolPort：Scheduler 只认 dispatch(call)，不关心工具从哪来。
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


def infer_schema(fn: Callable) -> dict:
    """从函数签名和类型注解，推断一份最简 JSON Schema（教学版，不引第三方）。"""
    sig = inspect.signature(fn)
    properties, required = {}, []
    # 约定：ctx 是框架注入的上下文，不是模型要填的参数，跳过。
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
    # read=只读可放心并行/重试；write=有副作用，执行前过审批门。
    side_effect: str = "read"


class ToolRegistry:
    """工具注册表 + 受治理的执行管线（它就是一个 ToolPort）。"""

    def __init__(self, *, bus: Any = None, write_needs_approval: bool = True):
        self._tools: dict[str, ToolSpec] = {}
        self.bus = bus
        self.write_needs_approval = write_needs_approval

    # —— 注册 ——
    def add(self, spec: ToolSpec) -> ToolRegistry:
        if spec.name in self._tools:
            raise ValueError(f"工具重名：{spec.name}")
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
        """把一个普通 Python 函数注册成工具，schema 自动从签名推断。"""
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

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict]:
        """给模型看的 function-calling 工具清单。"""
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

    # —— 受治理的执行 ——
    async def dispatch(self, call: ToolCall, ctx: Any = None) -> ToolResult:
        spec = self._tools.get(call.name)
        if spec is None:
            return ToolResult.failure(f"没有这个工具：{call.name}", call.call_id)

        missing = [k for k in spec.parameters.get("required", []) if k not in call.arguments]
        if missing:
            # 参数错误是“反馈”而不是崩溃：把缺什么告诉模型，让它下一轮改对。
            return ToolResult.failure(f"缺少必填参数：{missing}", call.call_id)

        if spec.side_effect == "write" and self.write_needs_approval and self.bus is not None:
            verdict = await self.bus.check(f"tool:{spec.name}", call=call, ctx=ctx)
            if not verdict.allowed:
                return ToolResult.failure(f"操作未获批准：{verdict.reason}", call.call_id)

        try:
            result = self._invoke(spec.func, call.arguments, ctx)
            if inspect.isawaitable(result):
                result = await result
            return ToolResult.success(result, call.call_id)
        except Exception as exc:  # 工具异常也变成可回喂的反馈，而不是炸穿整图
            return ToolResult.failure(f"{type(exc).__name__}: {exc}", call.call_id)

    @staticmethod
    def _invoke(fn: Callable, arguments: dict, ctx: Any) -> Any:
        # 两种入参约定都支持：
        # - 普通业务函数：参数就是模型要填的字段（weather(city, ctx)），按名注入；
        # - 适配类函数（如 MCP caller）：只有一个 arguments/args/payload 形参，整包传入。
        # 名为 ctx 的参数不由模型填，由框架注入。
        sig = inspect.signature(fn)
        data_params = [n for n in sig.parameters if n not in ("ctx", "_ctx", "context")]
        kwargs: dict[str, Any] = {}
        if len(data_params) == 1 and data_params[0] in ("arguments", "args", "payload"):
            kwargs[data_params[0]] = arguments
        else:
            for name, p in sig.parameters.items():
                if name in ("ctx", "_ctx", "context"):
                    kwargs[name] = ctx
                elif name in arguments:
                    kwargs[name] = arguments[name]
                elif p.default is inspect.Parameter.empty:
                    raise TypeError(f"缺少参数：{name}")
                else:
                    kwargs[name] = p.default
        if any(n in ("ctx", "_ctx", "context") for n in sig.parameters):
            ctx_name = next(n for n in sig.parameters if n in ("ctx", "_ctx", "context"))
            kwargs[ctx_name] = ctx
        return fn(**kwargs)
