"""Trust chain defence — RAG write-time guard + bot-to-bot handoff contract."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from prodagent.core.exceptions import PromptInjectionDetected, SecurityViolation

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


class KnowledgeBaseWriteGuard:
    """Intercepts poisoned documents at write time, not query time.

    ``patterns`` are app policy (raw strings compiled case-insensitively, or
    pre-compiled patterns); with none configured the pattern check is skipped
    and only the imperative-density heuristic runs. The heuristic is mechanism
    — generic linguistics, no domain vocabulary — so it stays always-on.
    """

    _IMPERATIVE_WORDS = re.compile(
        r"\b(?:ignore|forget|override|bypass|disregard)\b",
        re.IGNORECASE,
    )
    _INSTRUCTION_CUE = re.compile(
        r"\b(?:you|your|yours|yourself|instructions?|prompts?|rules?|"
        r"system\s+prompt|agent\s+instructions?|assistant\s+rules?)\b",
        re.IGNORECASE,
    )
    # Reduce false positives in K8s/SQL/shell docs
    _TECHNICAL_EXCLUSIONS = re.compile(
        r"\b(?:execute\s+(?:query|plan|statement)|do\s+(?:while|until)|"
        r"run\s+(?:tests?|migrations?|scripts?))\b",
        re.IGNORECASE,
    )
    _SENTENCE_SPLIT = re.compile(r"[.!?\n。！？]+")

    IMPERATIVE_DENSITY_THRESHOLD = 0.10

    def __init__(self, patterns: Sequence[str | re.Pattern[str]] = ()) -> None:
        self._compiled: list[re.Pattern[str]] = [
            re.compile(p, re.IGNORECASE) if isinstance(p, str) else p for p in patterns
        ]

    def guard_document(self, doc: str, source: str = "unknown") -> None:
        for pattern in self._compiled:
            if pattern.search(doc):
                raise PromptInjectionDetected(
                    f"Poisoned document rejected at write time (source={source!r})",
                    source=source,
                    pattern=pattern.pattern[:60],
                )

        density = self._max_sentence_imperative_density(doc)
        has_cue = bool(self._INSTRUCTION_CUE.search(doc))
        if density > self.IMPERATIVE_DENSITY_THRESHOLD and has_cue:
            raise SecurityViolation(
                f"Document flagged for human review (source={source!r}): "
                f"sentence-level imperative density {density:.1%} > threshold "
                f"{self.IMPERATIVE_DENSITY_THRESHOLD:.1%} (with instruction cue present)",
                source=source,
                density=density,
            )

    def _max_sentence_imperative_density(self, text: str) -> float:
        if not text or not text.strip():
            return 0.0
        clean = self._TECHNICAL_EXCLUSIONS.sub("", text)
        sentences = self._SENTENCE_SPLIT.split(clean)
        max_density = 0.0
        for sent in sentences:
            words = re.findall(r"\w+", sent)
            if not words:
                continue
            matches = self._IMPERATIVE_WORDS.findall(sent)
            density = len(matches) / len(words)
            if density > max_density:
                max_density = density
        return max_density


def validate_handoff_security(
    data: dict[str, Any],
    *,
    allowed_actions: frozenset[str],
) -> None:
    """Semantic injection gate — runs in series with MessageContract.validate.

    ``allowed_actions`` is the app's action vocabulary — required, no default.
    """
    for fld in ("status", "result_data", "next_action"):
        if fld not in data:
            raise SecurityViolation(
                f"Agent handoff missing required field '{fld}' — rejected",
                field=fld,
            )

    if data.get("raw_llm_output") is not None:
        raise SecurityViolation(
            "Agent handoff contains raw_llm_output — free-text cross-agent "
            "passthrough is forbidden (injection highway)",
            field="raw_llm_output",
        )

    action = data.get("next_action", "")
    if action not in allowed_actions:
        raise SecurityViolation(
            f"Agent handoff next_action={action!r} not whitelisted — "
            f"suspected injection payload. Allowed: {sorted(allowed_actions)}",
            next_action=action,
            allowed=sorted(allowed_actions),
        )
