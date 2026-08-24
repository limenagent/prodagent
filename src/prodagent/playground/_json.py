"""Shared JSON coercion for SSE payloads."""

from __future__ import annotations

import dataclasses
import enum
import logging
from pathlib import PurePath
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["jsonable"]


def jsonable(obj: Any) -> Any:
    """Recursively coerce *obj* to JSON-serializable primitives."""
    if obj is None or isinstance(obj, bool | int | float | str):
        return obj
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, PurePath):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set | frozenset):
        return [jsonable(v) for v in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        try:
            return jsonable(obj.model_dump(mode="json"))
        except Exception:  # noqa: BLE001 — serialization falls back to repr
            logger.warning(
                "jsonable: model_dump() failed for %r; falling back to repr",
                type(obj).__name__,
                exc_info=True,
            )
    return repr(obj)
