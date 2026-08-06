"""Payload scrubbing — redact sensitive values before they reach the audit log."""

from __future__ import annotations

import re
from typing import Any

_REDACTED = "[REDACTED]"

_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pass",
        "secret",
        "client_secret",
        "api_key",
        "apikey",
        "api_token",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "auth_token",
        "signing_key",
        "private_key",
        "encryption_key",
        "ssn",
        "social_security_number",
        "credit_card",
        "card_number",
        "cvv",
        "pan",
    }
)

_DEFAULT_PATTERNS: list[str] = [
    r"1[3-9]\d{9}",
    r"\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]",
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
    r"\b\d{3}-\d{2}-\d{4}\b",
    r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2})[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b",
    r"sk-[a-zA-Z0-9]{20,}",
    r"sk-ant-[a-zA-Z0-9\-_]{20,}",
    r"ghp_[a-zA-Z0-9]{36}",
    r"ghs_[a-zA-Z0-9]{36}",
    r"AKIA[0-9A-Z]{16}",
    r"(?:password|passwd|secret|api_key|token)\s*[=:]\s*[\"']?\S{8,}",
]


def _compile(patterns: list[str]) -> re.Pattern[str]:
    return re.compile("(?:" + "|".join(patterns) + ")")


class DefaultScrubber:
    """Opt-in PII/secret redaction — pass explicitly to AuditLogger(scrubber=...)."""

    def __init__(
        self,
        *,
        extra_keys: frozenset[str] | None = None,
        patterns: list[str] | None = None,
    ) -> None:
        self._keys = _SENSITIVE_KEYS | (extra_keys or frozenset())
        self._secret_pattern = _compile(patterns or _DEFAULT_PATTERNS)

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
