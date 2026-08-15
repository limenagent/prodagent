from __future__ import annotations

import inspect
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, overload

from pydantic import TypeAdapter

from prodagent.core.types import SideEffectLevel, ToolMeta
from prodagent.tooling.base import FunctionTool, _typed_params

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.core.aliases import ToolSchema

logger = logging.getLogger(__name__)

__all__ = ["tool"]

# Keys pydantic emits that we strip to keep the tool schema minimal
_SCHEMA_STRIP_KEYS: frozenset[str] = frozenset({"title", "$defs", "default"})


def _resolve_refs(schema: Any, defs: dict[str, Any], _depth: int = 0) -> Any:
    if _depth > 16:
        return schema
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = defs.get(ref.removeprefix("#/$defs/"))
            if target is not None:
                return _resolve_refs(target, defs, _depth + 1)
        return {k: _resolve_refs(v, defs, _depth + 1) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_resolve_refs(item, defs, _depth + 1) for item in schema]
    return schema


def _strip_schema_extras(schema: Any) -> Any:
    if isinstance(schema, dict):
        defs = schema.get("$defs", {})
        if defs:
            schema = _resolve_refs(schema, defs)
        return {
            k: _strip_schema_extras(v) for k, v in schema.items() if k not in _SCHEMA_STRIP_KEYS
        }
    if isinstance(schema, list):
        return [_strip_schema_extras(item) for item in schema]
    return schema


@overload
def tool(
    _fn: Callable[..., Any],
    *,
    name: str | None = ...,
    description: str | None = ...,
    readonly: bool = ...,
    meta: ToolMeta | None = ...,
) -> FunctionTool: ...


@overload
def tool(
    _fn: None = None,
    *,
    name: str | None = ...,
    description: str | None = ...,
    readonly: bool = ...,
    meta: ToolMeta | None = ...,
) -> Callable[[Callable[..., Any]], FunctionTool]: ...


def tool(
    _fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    readonly: bool = False,
    meta: ToolMeta | None = None,
) -> FunctionTool | Callable[[Callable[..., Any]], FunctionTool]:
    def _make(fn: Callable[..., Any]) -> FunctionTool:
        tool_name = name or (meta.name if meta else fn.__name__)
        doc = description or (inspect.getdoc(fn) or "")

        if meta is not None:
            if readonly and meta.side_effect_level in (
                SideEffectLevel.MEDIUM,
                SideEffectLevel.HIGH,
            ):
                raise ValueError(
                    f"@tool {tool_name!r}: readonly=True is incompatible with "
                    f"side_effect_level={meta.side_effect_level.value}. A readonly tool "
                    "must be LOW side-effect. Drop readonly=True or set side_effect_level=LOW."
                )
            base_meta = replace(
                meta,
                name=tool_name,
                is_readonly=meta.is_readonly or readonly,
                side_effect_level=(SideEffectLevel.LOW if readonly else meta.side_effect_level),
            )
        else:
            base_meta = ToolMeta(
                name=tool_name,
                is_readonly=readonly,
                side_effect_level=SideEffectLevel.LOW,
            )

        schema = _infer_schema(fn, tool_name, doc)
        return FunctionTool(name=tool_name, fn=fn, meta=base_meta, schema=schema)

    if _fn is not None:
        return _make(_fn)
    return _make


def _infer_schema(fn: Callable[..., Any], name: str, description: str) -> ToolSchema:
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param, ann in _typed_params(fn, name):
        if ann is inspect.Parameter.empty:
            properties[param_name] = {"type": "string"}
        else:
            try:
                raw = TypeAdapter(ann).json_schema()
            except Exception:
                logger.warning(
                    "Could not infer JSON schema for parameter %r of tool %r; "
                    "falling back to string",
                    param_name,
                    name,
                )
                raw = {"type": "string"}
            properties[param_name] = _strip_schema_extras(raw)
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }
