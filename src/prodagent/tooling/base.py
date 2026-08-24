from __future__ import annotations

import inspect
import logging
import typing
from typing import Any

from pydantic import TypeAdapter, ValidationError

from prodagent.core.error_reason import ErrorReason
from prodagent.kernel.types import ToolError, ToolMeta, ToolName, ToolResult

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from prodagent.core.aliases import ToolParams, ToolSchema

logger = logging.getLogger(__name__)

__all__ = ["FunctionTool"]


def _typed_params(
    fn: Callable[..., Any], context: str
) -> Iterator[tuple[str, inspect.Parameter, Any]]:
    """Yield (name, param, type hint) for each real parameter, skipping self/*args/**kwargs."""
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except Exception:
        logger.warning(
            "get_type_hints failed for %r; type info unavailable", context, exc_info=True
        )
        hints = {}
    for name, param in inspect.signature(fn).parameters.items():
        if name == "self" or param.kind in (
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            continue
        yield name, param, hints.get(name, inspect.Parameter.empty)


class FunctionTool:
    __slots__ = (
        "name",
        "meta",
        "schema",
        "_fn",
        "_is_coroutine",
        "_has_var_keyword",
        "_valid_params",
        "_required_params",
        "_adapters",
        "inject_run_id",
    )

    def __init__(
        self,
        name: ToolName,
        fn: Callable[..., Any],
        meta: ToolMeta,
        schema: ToolSchema,
        *,
        inject_run_id: bool = False,
    ) -> None:
        self.name = name
        self._fn = fn
        self.meta = meta
        self.schema = schema
        self.inject_run_id = inject_run_id
        self._is_coroutine = inspect.iscoroutinefunction(fn)
        self._cache_signature(fn)
        self._adapters = _build_adapters(fn, name)

    def _cache_signature(self, fn: Callable[..., Any]) -> None:
        sig = inspect.signature(fn)
        self._has_var_keyword = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if self._has_var_keyword:
            self._valid_params: frozenset[str] = frozenset()
            self._required_params: tuple[str, ...] = ()
        else:
            self._valid_params = frozenset(p for p in sig.parameters if p != "self")
            self._required_params = tuple(
                p
                for p, param in sig.parameters.items()
                if p != "self" and param.default is inspect.Parameter.empty
            )

    async def __call__(self, *, run_id: str = "", **kwargs: Any) -> ToolResult:
        if self.inject_run_id and "run_id" not in kwargs:
            kwargs["run_id"] = run_id
        kwargs = self._coerce_params(kwargs)

        if not self._has_var_keyword:
            unexpected = sorted(set(kwargs) - self._valid_params)
            missing = sorted(set(self._required_params) - set(kwargs))
            if unexpected or missing:
                parts = []
                if unexpected:
                    parts.append(f"unexpected: {unexpected}")
                if missing:
                    parts.append(f"missing required: {missing}")
                return ToolResult.from_error(
                    ToolError.from_reason(
                        ErrorReason.FORMAT_ERROR,
                        code="invalid_parameters",
                        message=(
                            f"Tool '{self.name}' called with wrong parameters - {'; '.join(parts)}."
                        ),
                        hint=(
                            f"Valid parameters for '{self.name}': {sorted(self._valid_params)}. "
                            f"Required: {list(self._required_params)}. "
                            "Do not pass parameters that belong to other tools."
                        ),
                    ),
                    tool=self.name,
                )

        if self._is_coroutine:
            raw = await self._fn(**kwargs)
        else:
            raw = self._fn(**kwargs)
        return ToolResult.from_raw(raw, tool=self.name)

    def _coerce_params(self, kwargs: ToolParams) -> ToolParams:
        if not self._adapters:
            return kwargs
        coerced = dict(kwargs)
        for param_name, adapter in self._adapters.items():
            if param_name not in coerced:
                continue
            val = coerced[param_name]
            try:
                coerced[param_name] = adapter.validate_python(val)
            except ValidationError:
                logger.warning(
                    "Tool %r: could not coerce parameter %r from %s; passing raw value",
                    self.name,
                    param_name,
                    type(val).__name__,
                )
        return coerced


def _build_adapters(fn: Callable[..., Any], tool_name: str) -> dict[str, TypeAdapter[Any]]:
    """Build a TypeAdapter per typed parameter, cached for the tool's lifetime."""
    adapters: dict[str, TypeAdapter[Any]] = {}
    for param_name, _param, ann in _typed_params(fn, tool_name):
        if ann is inspect.Parameter.empty:
            continue
        try:
            adapters[param_name] = TypeAdapter(ann)
        except Exception:
            logger.warning(
                "Could not build TypeAdapter for parameter %r of tool %r; "
                "value will be passed through uncoerced",
                param_name,
                tool_name,
                exc_info=True,
            )
    return adapters
