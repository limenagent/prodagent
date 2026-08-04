"""Constitutional AI — principle-based output self-check."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from prodagent.llm.base import LLMConfig, noop_chunk

if TYPE_CHECKING:
    from prodagent.core.types import MessageList
    from prodagent.llm.base import LLMClient

CritiqueFn = Callable[[str], bool]


@dataclass
class ConstitutionalPrinciple:
    """A single output rule: a violation predicate + a revision instruction."""

    name: str
    critique: CritiqueFn
    revision: str


@dataclass
class ConstitutionalResult:
    """Outcome of checking one output against the principle set."""

    revised: str
    violations: list[str] = field(default_factory=list)


class ConstitutionalChecker:
    """Apply each principle to an output; flag and (optionally) revise violations."""

    def __init__(
        self,
        principles: list[ConstitutionalPrinciple],
        *,
        llm: LLMClient | None = None,
    ) -> None:
        if not principles:
            raise ValueError("At least one principle is required")
        self._principles = principles
        self._llm = llm

    async def check(self, output: str) -> ConstitutionalResult:
        """Apply all principles to *output*, revising on each violation."""
        current = output
        violations: list[str] = []

        for principle in self._principles:
            if principle.critique(current):
                violations.append(principle.name)
                if self._llm is not None:
                    current = await self._revise(current, principle)

        return ConstitutionalResult(
            revised=current,
            violations=violations,
        )

    async def _revise(self, output: str, principle: ConstitutionalPrinciple) -> str:
        assert self._llm is not None
        system = (
            "You are revising an AI response to satisfy a specific requirement. "
            "Apply the minimum changes needed. Return ONLY the revised response."
        )
        prompt = f"Revision instruction: {principle.revision}\n\nResponse:\n{output}"
        messages: MessageList = [{"role": "user", "content": prompt}]
        response = await self._llm.complete(
            messages,
            system=system,
            config=LLMConfig(temperature=0.0, max_tokens=1024),
            on_chunk=noop_chunk,
        )
        return response.content.strip()


__all__ = [
    "ConstitutionalChecker",
    "ConstitutionalPrinciple",
    "ConstitutionalResult",
]
