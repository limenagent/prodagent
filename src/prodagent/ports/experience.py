"""ExperienceStore port — append-only journal of completed agent runs.

Used by the closed learning loop (``LearningHooks`` → ``SkillSynthesizer``)
to distill successful runs into patched skills. ``file`` is the local JSONL
default; ``postgres`` aggregates across replicas in production.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.evaluation.learning.experience import ExperienceRecord


@runtime_checkable
class ExperienceStore(Protocol):
    def record(self, record: ExperienceRecord) -> None: ...

    def load_all(self) -> list[ExperienceRecord]: ...
