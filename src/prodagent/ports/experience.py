"""ExperienceStore port + its payload record.

Used by the closed learning loop (``LearningHooks`` → ``SkillSynthesizer``)
to distill successful runs into patched skills. ``file`` is the local JSONL
default; ``postgres`` aggregates across replicas in production. The record
type lives with the port (ports must not import feature packages).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from prodagent.kernel.types import RunState

if TYPE_CHECKING:
    from prodagent.kernel.state import AgentRun


@runtime_checkable
class ExperienceStore(Protocol):
    async def record(self, record: ExperienceRecord) -> None: ...

    async def load_all(self) -> list[ExperienceRecord]: ...


def conversation_messages(run: AgentRun) -> list[dict[str, Any]]:
    """Copy of the run transcript; falls back to the seed task when empty."""
    if run.messages:
        return [dict(m) for m in run.messages]
    return [{"role": "user", "content": run.task}]


class ExperienceOutcome(StrEnum):
    SUCCESS = "success"  # agent completed task, final_output non-empty
    FAILURE = "failure"  # agent failed or hit budget limit
    PARTIAL = "partial"  # agent stopped mid-task (e.g. human approval denied)


@dataclass
class ExperienceRecord:
    """Serialisable snapshot of one completed agent run."""

    run_id: str
    task: str
    outcome: ExperienceOutcome
    tool_sequence: list[str]
    final_output: str | None
    cost_usd: float
    turn_count: int
    elapsed_seconds: float
    tags: list[str]
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    session_transcript: list[dict[str, Any]] = field(default_factory=list)

    def to_jsonl(self) -> str:
        d = asdict(self)
        d["outcome"] = self.outcome.value
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExperienceRecord:
        d = dict(d)
        d["outcome"] = ExperienceOutcome(d["outcome"])
        d.setdefault("session_transcript", [])
        return cls(**d)

    @classmethod
    def from_run(cls, run: AgentRun, *, tags: list[str] | None = None) -> ExperienceRecord:
        """Build a record from a completed ``AgentRun``.

        ``tags`` overrides the keyword tagger; ``None`` derives tags from
        ``run.task``. Transcript is the full conversation — the synthesiser
        applies size bounds when it builds the LLM prompt.
        """
        return cls(
            run_id=run.run_id,
            task=run.task,
            outcome=_outcome_for(run),
            tool_sequence=[tc.name for tc in run.tool_history],
            final_output=run.final_output,
            cost_usd=run.cost_usd,
            turn_count=run.turn_count,
            elapsed_seconds=run.elapsed_seconds(),
            tags=tags if tags is not None else _extract_tags(run.task),
            session_transcript=conversation_messages(run),
        )


def _outcome_for(run: AgentRun) -> ExperienceOutcome:
    if run.state == RunState.COMPLETED:
        output = str(run.final_output or "").strip()
        if not output:
            return ExperienceOutcome.PARTIAL
        if not run.tool_history:
            return ExperienceOutcome.PARTIAL
        return ExperienceOutcome.SUCCESS
    if run.state == RunState.FAILED:
        return ExperienceOutcome.FAILURE
    return ExperienceOutcome.PARTIAL


_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "has",
        "have",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "need",
        "dare",
        "to",
        "for",
        "in",
        "on",
        "at",
        "by",
        "of",
        "with",
        "from",
        "and",
        "or",
        "but",
        "not",
    ]
)


def _extract_tags(text: str, *, max_tags: int = 8) -> list[str]:
    """Stopword-filtered keyword tags, ordered by first occurrence.

    CJK-aware: Chinese text is tokenized into 2-grams + 3-grams so that
    'reboot Pod' and 'reboot Deployment' share the reboot tag. ASCII words are
    split normally.
    """
    from prodagent.core.text import tokenize_cjk

    tokens = tokenize_cjk(text)
    seen: dict[str, None] = {}
    for tok in tokens:
        if tok in _STOPWORDS:
            continue
        seen.setdefault(tok, None)
    return list(seen)[:max_tags]
