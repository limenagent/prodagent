"""Payload scrubbing — redact sensitive values before they reach the audit log.

Keys and patterns are app policy: pass them explicitly to
``PatternScrubber(keys=..., patterns=...)``. Empty (the default) redacts
nothing — audit payloads pass through unless the app opts into redaction.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

_REDACTED = "[REDACTED]"

_NEVER_MATCHES = re.compile(r"(?!)")


def _compile(patterns: Sequence[str]) -> re.Pattern[str]:
    if not patterns:
        return _NEVER_MATCHES
    return re.compile("(?:" + "|".join(patterns) + ")")


class PatternScrubber:
    """Pattern-based redaction — keys + patterns are app policy.

    ``keys`` matches payload keys case-insensitively; ``patterns`` matches
    string values (compiled jointly). Empty configuration redacts nothing.
    """

    def __init__(
        self,
        *,
        keys: frozenset[str] = frozenset(),
        patterns: Sequence[str] = (),
    ) -> None:
        self._keys = keys
        self._secret_pattern = _compile(patterns)

    def scrub(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {k: self._scrub_value(k, v) for k, v in payload.items()}

    def scrub_any(self, value: Any) -> Any:
        return self._scrub_value("", value)

    def _scrub_value(self, key: str, value: Any) -> Any:
        if key.lower() in self._keys:
            return _REDACTED
        if isinstance(value, str) and self._secret_pattern.search(value):
            return _REDACTED
        if isinstance(value, dict):
            return self.scrub(value)
        if isinstance(value, list):
            return [
                self.scrub(item) if isinstance(item, dict) else self._scrub_value("", item)
                for item in value
            ]
        return value


class PassthroughScrubber:
    """No-op scrubber — payloads pass through unmodified."""

    def scrub(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    def scrub_any(self, value: Any) -> Any:
        return value
