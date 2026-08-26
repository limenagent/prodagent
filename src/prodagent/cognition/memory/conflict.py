from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from prodagent.base.time import now_timestamp
from prodagent.cognition.memory.channels import top_k_by_cosine
from prodagent.cognition.memory.storage import MemoryRecord, StoredMemory, mem_id
from prodagent.llm import LLMConfig, stream_text
from prodagent.llm.structured_output import extract_json_object

if TYPE_CHECKING:
    from prodagent.cognition.memory.embedder import HashEmbedder
    from prodagent.llm import LLMClient
    from prodagent.ports.document import DocumentStore

logger = logging.getLogger(__name__)

_DEFAULT_TOP_K = 10
# HashEmbedder cosine is conservative; real embedders score 0.6-0.8 and need
# this floor raised to ~0.45.
_DEFAULT_MIN_COSINE = 0.25

__all__ = [
    "EmbeddingCandidateFilter",
    "DefaultConflictPolicy",
    "ConflictVerdict",
    "SupersedeAction",
]


@dataclass(frozen=True)
class ConflictVerdict:
    winner: StoredMemory
    loser: StoredMemory
    reason: str = "contradiction"


class EmbeddingCandidateFilter:
    """Embedding similarity Top-K — cheap conflict-candidate retrieval."""

    def __init__(
        self,
        embedder: HashEmbedder,
        *,
        top_k: int = _DEFAULT_TOP_K,
        min_cosine: float = _DEFAULT_MIN_COSINE,
    ) -> None:
        self._embedder = embedder
        self._top_k = top_k
        self._min_cosine = min_cosine

    async def candidates(self, new_mem: StoredMemory, store: DocumentStore) -> list[StoredMemory]:
        query_vec = (
            new_mem.embedding
            if new_mem.embedding is not None
            else self._embedder.embed(new_mem.content)
        )
        pool = [m for m in await store.load_memories() if m.memory_type == new_mem.memory_type]
        scored = top_k_by_cosine(
            query_vec,
            pool,
            self._embedder,
            k=self._top_k,
            min_cosine=self._min_cosine,
            domain=new_mem.domain,
            exclude_id=new_mem.id,
        )
        return [mem for _, mem in scored]


class DefaultConflictPolicy:
    """LLM batched conflict confirmation."""

    _SYSTEM_PROMPT = """\
You are a memory dedup + conflict detector. Each candidate pair was pre-filtered
by embedding similarity; decide if they are truly redundant.

Flag a pair when:

1. DUPLICATE — same fact/rule/event, even if reworded or one adds detail the
   other lacks (e.g. "PR #4412 caused OOM" vs "PR #4412 (SHA a3f92b1) caused
   OOM and cascaded to checkout"). Keep the more complete/precise one; discard
   the other.
2. CONTRADICTION — the memories disagree (e.g. "user likes VIP" vs "user
   downgraded to basic"). The newer one wins.

Do NOT flag pairs that merely share a topic but state distinct facts (e.g.
"payment-service has an OOM bug" vs "payment-service deploys on Fridays" —
same service, different knowledge).

Return JSON only (no prose, no fences):
{"conflicts": [
   {"winner_id": "<keep>", "loser_id": "<discard>",
    "reason": "duplicate" | "contradiction" | "superseded_by"}
]}
No conflicts → {"conflicts": []}"""

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        *,
        batch_size: int = 20,
        model: str | None = None,
    ) -> None:
        self._llm = llm_client
        self._batch_size = batch_size
        self._model = model

    async def confirm_conflicts(
        self, candidate_pairs: list[tuple[StoredMemory, StoredMemory]]
    ) -> list[ConflictVerdict]:
        if self._llm is None or not candidate_pairs:
            return []

        verdicts: list[ConflictVerdict] = []
        for i in range(0, len(candidate_pairs), self._batch_size):
            batch = candidate_pairs[i : i + self._batch_size]
            verdicts.extend(await self._confirm_batch(batch))
        return verdicts

    async def _confirm_batch(
        self, batch: list[tuple[StoredMemory, StoredMemory]]
    ) -> list[ConflictVerdict]:
        if not batch:
            return []

        by_id: dict[str, StoredMemory] = {}
        for a, b in batch:
            by_id.setdefault(a.id, a)
            by_id.setdefault(b.id, b)

        pairs_block = "\n".join(
            f"- pair {i}: [{a.id}] {a.content[:200]}  VS  [{b.id}] {b.content[:200]}"
            for i, (a, b) in enumerate(batch)
        )
        prompt = f"Candidate pairs:\n{pairs_block}\n\nWhich pairs truly CONFLICT?"

        assert self._llm is not None
        try:
            response, text = await stream_text(
                self._llm,
                messages=[{"role": "user", "content": prompt}],
                system=self._SYSTEM_PROMPT,
                config=LLMConfig(model=self._model or "", max_tokens=4096, temperature=0.0),
                include_reasoning=True,
            )
            parsed = self._parse_response(response, text)
            if parsed is None:
                return []
        except (KeyError, TypeError, AttributeError) as exc:
            logger.warning("[memory] conflict policy parse failed: %s — skipping batch", exc)
            return []

        verdicts: list[ConflictVerdict] = []
        for entry in parsed.get("conflicts", []):
            winner = by_id.get(entry.get("winner_id", ""))
            loser = by_id.get(entry.get("loser_id", ""))
            if winner is None or loser is None:
                logger.warning(
                    "[memory] conflict policy: LLM returned unknown id pair "
                    "(winner=%s loser=%s) — skipping",
                    entry.get("winner_id"),
                    entry.get("loser_id"),
                )
                continue

            reason = entry.get("reason", "contradiction")
            # For non-duplicate verdicts the newer memory wins; swap if the
            # LLM inverted the age.
            if (
                reason != "duplicate"
                and winner.created_at
                and loser.created_at
                and winner.created_at < loser.created_at
            ):
                logger.info(
                    "[memory] conflict policy: LLM inverted verdict age "
                    "(winner=%s @ %s older than loser=%s @ %s, reason=%s) — swapping",
                    winner.id,
                    winner.created_at,
                    loser.id,
                    loser.created_at,
                    reason,
                )
                winner, loser = loser, winner

            verdicts.append(ConflictVerdict(winner=winner, loser=loser, reason=reason))
        return verdicts

    def _parse_response(self, response: Any, text: str) -> dict[str, Any] | None:
        """Extract + parse the LLM's JSON. Returns ``None`` on any failure."""
        try:
            extracted = extract_json_object(text)
        except ValueError as exc:
            logger.warning(
                "[memory] conflict policy extract failed "
                "(stop_reason=%s, output_tokens=%s) — %s: %s\n  snippet_tail=%r",
                response.stop_reason,
                response.output_tokens,
                type(exc).__name__,
                exc,
                text[-200:],
            )
            return None
        try:
            parsed = json.loads(extracted)
        except json.JSONDecodeError as exc:
            logger.warning(
                "[memory] conflict policy json.loads failed "
                "(stop_reason=%s, output_tokens=%s) — %s: %s\n  snippet_tail=%r",
                response.stop_reason,
                response.output_tokens,
                type(exc).__name__,
                exc,
                extracted[-200:],
            )
            return None
        if not isinstance(parsed, dict):
            logger.warning(
                "[memory] conflict policy expected object, got %s "
                "(stop_reason=%s, output_tokens=%s)\n  snippet_tail=%r",
                type(parsed).__name__,
                response.stop_reason,
                response.output_tokens,
                extracted[-200:],
            )
            return None
        return parsed


class SupersedeAction:
    """Mark the loser ``superseded=True`` (reversible, preserves provenance)."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    async def resolve(self, winner: StoredMemory, loser: StoredMemory, reason: str) -> None:
        await self._store.mark_superseded(loser.id, True)
        logger.info(
            "[memory] supersede: %s superseded by %s (%s)",
            loser.id,
            winner.id,
            reason,
        )


def _id(content: str) -> str:
    return mem_id(content, prefix="transient:")


class ConflictPipeline:
    """Compose the three conflict stages into one write-time call."""

    def __init__(
        self,
        candidate_filter: EmbeddingCandidateFilter,
        policy: DefaultConflictPolicy | None,
        action: SupersedeAction,
    ) -> None:
        self._filter = candidate_filter
        self._policy = policy
        self._action = action

    async def resolve(self, new_record: MemoryRecord, store: DocumentStore) -> bool:
        """Returns True when the new memory was discarded as a duplicate."""
        if self._policy is None:
            return False

        transient = StoredMemory.from_record(
            new_record,
            id=_id(new_record.content),
            created_at=now_timestamp(),
        )

        candidates = await self._filter.candidates(transient, store)
        if not candidates:
            return False

        new_mem_discarded = False
        candidate_pairs = [(transient, old) for old in candidates]
        for verdict in await self._policy.confirm_conflicts(candidate_pairs):
            if verdict.loser.id == transient.id:
                new_mem_discarded = True
                logger.info(
                    "[memory] incremental: new memory discarded — existing %s "
                    "already captures this (%s)",
                    verdict.winner.id,
                    verdict.reason,
                )
                continue
            await self._action.resolve(verdict.winner, verdict.loser, verdict.reason)
        return new_mem_discarded
