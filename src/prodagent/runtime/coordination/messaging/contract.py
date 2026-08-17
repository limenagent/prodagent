"""MessageContract — the declared shape an upstream crossing must satisfy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["MessageContract", "DEFAULT_CHILD_CONTRACT"]


@dataclass
class MessageContract:
    """Structural admission contract for a crossing payload.

    ``required_fields``/``optional_fields``/``field_types`` apply to Mapping
    payloads (a child result dict, a structured board value); ``value_type``
    applies to any payload (``str`` for a free-text board slot). Both unset on
    a non-mapping payload means nothing to check — pass-through.
    """

    required_fields: list[str] = field(default_factory=list)
    field_types: dict[str, type] = field(default_factory=dict)
    optional_fields: list[str] = field(default_factory=list)
    strict: bool = True
    """Strict → violation rejects the crossing; lenient → dead-letter + pass on."""

    value_type: type | None = None

    def validate(self, result: Mapping[str, Any] | Any) -> tuple[bool, str | None]:
        if self.value_type is not None and not isinstance(result, self.value_type):
            return (
                False,
                f"expected {self.value_type.__name__}, got {type(result).__name__}",
            )
        if not isinstance(result, Mapping):
            return True, None
        for name in self.required_fields:
            if name not in result:
                return False, f"missing required field {name!r}"
            expected = self.field_types.get(name)
            if expected is not None and not isinstance(result[name], expected):
                return (
                    False,
                    f"field {name!r} expected {expected.__name__}, "
                    f"got {type(result[name]).__name__}",
                )
        for name in self.optional_fields:
            if name in result:
                expected = self.field_types.get(name)
                if expected is not None and not isinstance(result[name], expected):
                    return (
                        False,
                        f"field {name!r} expected {expected.__name__}, "
                        f"got {type(result[name]).__name__}",
                    )
        return True, None

    def whitelist(self, result: Mapping[str, Any]) -> dict[str, Any]:
        """Project a Mapping down to declared fields — sanitize as you admit.

        Unknown keys never cross the boundary, so callers consume the *declared*
        view, not whatever the producing agent happened to emit.
        """
        allowed = set(self.required_fields) | set(self.optional_fields)
        return {k: v for k, v in result.items() if k in allowed}


DEFAULT_CHILD_CONTRACT = MessageContract(
    required_fields=["agent", "output", "state"],
    field_types={"agent": str, "output": str, "state": str},
)
"""Default admission for a spawned child's result dict — identity + verdict + text."""
