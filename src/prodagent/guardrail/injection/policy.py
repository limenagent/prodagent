"""Injection-defence policy — app-owned detection rules, pass-through by default.

The framework ships the scanning *mechanism* (the five-layer pipeline and its
hook checkpoints); which payloads count as injections or sensitive content is
*policy* and must be injected by the application. An ``InjectionPolicy`` with
empty pattern tuples disables detection entirely — nothing is scanned, nothing
is vetoed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import re


class OutputDisposition(StrEnum):
    """What L5 does when sensitive content is found in the final output."""

    OBSERVE = "observe"  # log findings, output unchanged (default)
    REDACT = "redact"  # rewrite matches to the placeholder
    VETO = "veto"  # raise SensitiveContentDetected


@dataclass(frozen=True)
class InjectionPolicy:
    """Detection rules for the five-layer pipeline — supplied by the app."""

    injection_patterns: tuple[re.Pattern[str], ...] = ()
    sensitive_patterns: tuple[re.Pattern[str], ...] = ()
    output_disposition: OutputDisposition = OutputDisposition.OBSERVE
    redaction_placeholder: str = "[REDACTED]"
