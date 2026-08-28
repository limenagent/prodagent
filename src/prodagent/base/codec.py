"""Field-driven (de)serialization for the framework's plain dataclasses.

The hand-written ``to_dict``/``from_dict`` pairs this module replaces were
all the same walk: dump in field order with enums as their values, load
with ``d.get(name, default)`` coercing enums and recursing into nested
dataclasses. Classes whose projection is *curated* — ``AgentRun``'s durable
subset, the event wire's ``type`` discriminator — keep their hand-written
methods; only mechanical mirrors delegate here.

Annotations drive loading (resolved lazily, so forward references work):
enum fields coerce by value, dataclass fields recurse, ``list[X]`` walks X,
``dict`` fields copy fresh with the falsy-tolerant ``or`` the replaced
loaders used. ``_raw`` names fields that pass through untouched (raw
message dicts); ``raw=`` and ``defaults=`` supply values for keys a wire
form may omit — dynamic per-load values and fixed wire defaults.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from collections.abc import Mapping
from enum import Enum
from typing import Any, get_args, get_origin, get_type_hints

__all__ = ["dump", "load"]


def dump(obj: Any, *, _raw: frozenset[str] = frozenset()) -> Any:
    """JSON-able form of a dataclass: field order, enums as values, nested
    dataclasses recursed, list/tuple elements walked. Plain dicts are
    shallow-copied, never walked — their values are already wire-shaped."""
    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            v = getattr(obj, f.name)
            out[f.name] = _copy_raw(v) if f.name in _raw else dump(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [dump(v) for v in obj]
    if isinstance(obj, Mapping):
        return dict(obj)
    return obj


def load(
    cls: type,
    d: Mapping[str, Any],
    *,
    raw: Mapping[str, Any] | None = None,
    defaults: Mapping[str, Any] | None = None,
    _raw: frozenset[str] = frozenset(),
) -> Any:
    """Rebuild a dataclass from its dumped form — the inverse of :func:`dump`.

    Per field: the key wins, then a ``raw=`` override, then the field's own
    default, then a ``defaults=`` wire default; a field with no default and
    no value raises ``KeyError``, the same contract the replaced
    ``d["name"]`` accessors had."""
    hints = _hints(cls)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name in d:
            value = d[f.name]
        elif raw is not None and f.name in raw:
            value = raw[f.name]
        elif defaults is not None and f.name in defaults:
            value = defaults[f.name]
        else:
            value = _required(f)
        kwargs[f.name] = _copy_raw(value) if f.name in _raw else _coerce(value, hints.get(f.name))
    return cls(**kwargs)


def _copy_raw(v: Any) -> Any:
    """Fresh container, same contents — mutation isolation without coercion."""
    if isinstance(v, dict):
        return dict(v)
    if isinstance(v, (list, tuple)):
        return list(v)
    return v


def _required(f: dataclasses.Field) -> Any:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return f.default_factory()  # type: ignore[misc]
    raise KeyError(f.name)


_HINTS: dict[type, dict[str, Any]] = {}


def _hints(cls: type) -> dict[str, Any]:
    """Resolved annotations, cached. Unresolvable forward references yield
    an empty map — those fields then load as-is, same as no annotation."""
    if cls not in _HINTS:
        try:
            _HINTS[cls] = get_type_hints(cls)
        except NameError:
            _HINTS[cls] = {}
    return _HINTS[cls]


_UNIONS = (typing.Union, types.UnionType)


def _coerce(value: Any, ann: Any) -> Any:
    if ann is None or ann is Any:
        return value
    origin = get_origin(ann)
    if origin in _UNIONS:
        args = [a for a in get_args(ann) if a is not type(None)]
        if value is None or not args:
            return value
        return _coerce(value, args[0])
    if isinstance(ann, type) and issubclass(ann, Enum):
        return value if isinstance(value, ann) else ann(value)
    if dataclasses.is_dataclass(ann) and isinstance(ann, type):
        return value if isinstance(value, ann) else load(ann, value)
    if origin is list:
        args = get_args(ann)
        elem = args[0] if args else None
        return [_coerce(v, elem) for v in (value or [])]
    if origin is dict:
        return dict(value or {})
    return value
