"""Durable-state stores — snapshots, sessions, and the memory corpus.

Family home for the book's persistence-and-memory socket family (checkpoint / session /
document / graph / experience, merged 2026-08). ``CheckpointStore`` and
``SessionStore`` are peers, not layers — a session spans many runs, a
checkpoint lives within one run (their optimistic-concurrency shapes match
on purpose). ``DocumentStore`` / ``GraphStore`` / ``ExperienceStore`` are
the memory channels: soft memories, the entity graph, and the distilled-
skill raw material.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from prodagent.base.codec import dump, load
from prodagent.base.determinism import now_wall
from prodagent.kernel.types import RunState

if TYPE_CHECKING:
    from prodagent.base.session import ConversationSession
    from prodagent.kernel.run import Run

# ════════════ from checkpoint.py ════════════


@runtime_checkable
class BlobStore(Protocol):
    """Content-addressed blob store — the object room for oversized facts.

    The boundary streams' pointer target: a fact too big
    for the hot log line lives here once, keyed by its sha256 digest; the
    log record holds only ``{"$blob": digest}``. Same content → same digest
    → stored once, shared by every projection that wants it (span and
    cassette both point at one body — "store the big body once" is the
    dedupe that keeps the fact pipeline affordable).

    Capabilities:
      BASE (required): put, get
    """

    async def put(self, text: str) -> str:
        """Store ``text`` under its sha256 hex digest, return the digest.

        Idempotent by construction — same content, same digest, one body.
        """
        ...

    async def get(self, digest: str) -> str | None:
        """The body for ``digest``, or ``None`` if absent (a miss is the
        normal path, never an exception)."""
        ...


@runtime_checkable
class CheckpointStore(Protocol):
    """Durable snapshot path — save and resume a run.

    Capabilities:
      BASE (required): save, load, list_run_ids
      EXTENDED (optional): fork, list_versions

    Implementations that lack EXTENDED capabilities must still accept calls
    to ``save`` with ``expected_version`` for optimistic concurrency.
    """

    async def save(self, run: Run, expected_version: int | None = None) -> None:
        """Idempotent atomic persist under ``run.run_id``.

        ``expected_version`` enables optimistic concurrency: raise
        ``VersionConflict`` if the stored version differs.
        """
        ...

    async def load(self, run_id: str, version: int | None = None) -> Run | None:
        """Return the ``Run`` for ``run_id``, or ``None`` if absent.

        ``version=None`` means latest; stores without version history may
        ignore it.
        """
        ...

    async def list_run_ids(self) -> list[str]:
        """All run ids with at least one checkpoint."""
        ...

    # --- EXTENDED capabilities (optional) -------------------------------

    async def fork(
        self,
        run_id: str,
        at_version: int,
        new_run_id: str | None = None,
    ) -> str:
        """Create a new run from a historical snapshot, return its id.

        Implementations may raise ``NotImplementedError`` if they do not
        keep version history.
        """
        ...

    async def list_versions(self, run_id: str) -> list[int]:
        """Versions available for ``run_id``, ascending. Empty if none."""
        ...


# ════════════ from session.py ════════════


@runtime_checkable
class SessionStore(Protocol):
    """Durable home for ``ConversationSession``.

    Same optimistic-concurrency API shape as ``CheckpointStore``/``EventLog``,
    but a different scope: a session spans many runs (one turn per
    ``Run``), while ``CheckpointStore``/``EventLog`` only ever track
    state *within* a single run. Peers, not layers — neither wraps the
    other.
    """

    async def save(self, session: ConversationSession, expected_version: int | None = None) -> None:
        """Idempotent atomic persist under ``session.session_id``.

        ``expected_version`` enables optimistic concurrency: raise
        ``VersionConflict`` if the stored version differs.
        """
        ...

    async def load(self, session_id: str) -> ConversationSession | None:
        """Return the session for ``session_id``, or ``None`` if absent."""
        ...


# ════════════ from document.py ════════════

MAX_SOFT_MEMORIES = 300
EPISODIC_DEFAULT_TTL_DAYS = 7


def mem_id(text: str, *, prefix: str = "") -> str:
    # Content-hash identity: writing the same memory twice lands on the same
    # id — dedupe and conflict supersession without a lookup query.
    h = hashlib.blake2b(
        text.encode(), digest_size=6
    ).hexdigest()  # 6 bytes: short, collision-safe enough
    return f"{prefix}{h}" if prefix else h


class MemoryType(StrEnum):
    CONSTRAINT = "constraint"
    FACT = "fact"
    PREFERENCE = "preference"
    EPISODIC = "episodic"


@dataclass
class MemoryRecord:
    """Write-side DTO — what a Classifier produces and a Store consumes."""

    content: str
    memory_type: MemoryType = MemoryType.EPISODIC
    entity_id: str = ""
    domain: str = "general"
    ttl_days: int | None = None
    source: str = ""
    embedding: list[float] | None = None

    def __post_init__(self) -> None:
        self.memory_type = _coerce_memory_type(self.memory_type)


@dataclass
class StoredMemory:
    """Persisted record — what lives in memories.json and what recall returns."""

    id: str
    content: str
    memory_type: MemoryType
    domain: str = "general"
    entity_id: str = ""
    ttl_days: int | None = None
    created_at: str = ""
    superseded: bool = False
    version: int = 1
    access_count: int = 0
    last_access: str = ""
    embedding: list[float] | None = None

    def __post_init__(self) -> None:
        self.memory_type = _coerce_memory_type(self.memory_type)

    @classmethod
    def from_record(cls, record: MemoryRecord, *, id: str, created_at: str) -> StoredMemory:
        return cls(
            id=id,
            content=record.content,
            memory_type=record.memory_type,
            domain=record.domain,
            entity_id=record.entity_id,
            ttl_days=record.ttl_days,
            created_at=created_at,
            embedding=record.embedding,
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StoredMemory:
        return load(
            cls,
            d,
            defaults={"id": "", "content": "", "memory_type": MemoryType.EPISODIC.value},
        )

    def to_dict(self) -> dict[str, Any]:
        return dump(self)


def _coerce_memory_type(value: Any) -> MemoryType:
    if isinstance(value, MemoryType):
        return value
    return MemoryType(str(value).lower())


@runtime_checkable
class DocumentStore(Protocol):
    """Storage for CONSTRAINT + PREFERENCE + EPISODIC memories."""

    async def load_constraints(self) -> list[StoredMemory]:
        """The always-on subset: constraints ride into every context build
        (recall-free — they gate behaviour, they aren't searched)."""
        ...

    async def load_memories(self) -> list[StoredMemory]:
        """Full corpus for recall — the embedder's working set."""
        ...

    async def save_memories(self, data: list[StoredMemory]) -> None:
        """Whole-corpus persist (consolidation writes back the full list)."""
        ...

    async def append_soft(self, record: MemoryRecord) -> None:
        """Append one soft memory — id is content-derived, so a re-write of
        the same content lands on the same record (dedupe by construction)."""
        ...

    async def mark_superseded(self, mem_id: str, superseded: bool) -> None:
        """Flag a memory as replaced — supersession, not deletion, so history
        stays auditable."""
        ...

    async def touch_memory(self, mem_id: str) -> None:
        """Record an access for recency scoring and decay."""
        ...


# ════════════ from graph.py ════════════


@runtime_checkable
class GraphStore(Protocol):
    """A directed property graph: nodes + typed edges + neighbour traversal."""

    async def add_node(
        self,
        node_id: str,
        labels: list[str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Insert or merge a node. Re-adding merges labels and properties."""
        ...

    async def add_edge(
        self,
        src: str,
        dst: str,
        rel: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Insert a directed edge ``src -[rel]-> dst``. Idempotent on (src,dst,rel)."""
        ...

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Return ``{id, labels, properties}`` or ``None`` if absent."""
        ...

    async def list_nodes(self, label: str | None = None) -> list[dict[str, Any]]:
        """All nodes, optionally filtered to those carrying ``label``.

        Each entry is ``{id, labels, properties}``. This is a full scan —
        recall uses it to surface every fact node; prefer ``neighbors`` when
        you have a starting point.
        """
        ...

    async def neighbors(
        self,
        node_id: str,
        rel: str | None = None,
        depth: int = 1,
    ) -> list[dict[str, Any]]:
        """Out-neighbours of ``node_id`` within ``depth`` hops, filtered to
        ``rel`` if given. Each entry is ``{id, labels, properties}``. An absent
        node returns ``[]``."""
        ...

    async def traverse(
        self, start: str, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Run a backend-native traversal query from ``start``.

        For Neo4j this is Cypher. Backends that don't support a query language
        may raise ``NotImplementedError`` — callers that need portability
        should stick to ``neighbors``.
        """
        ...

    async def delete_node(self, node_id: str) -> None:
        """Remove a node and its incident edges. No-op if missing."""
        ...

    # NOTE: the old ``upsert_fact`` / ``load_facts`` / ``save_facts`` methods
    # are gone. They were not graph operations. Callers that stored FACT
    # memories should use ``add_node`` (entity as node, facts as properties)
    # and ``add_edge`` to link related entities.


# ════════════ from experience.py ════════════


@runtime_checkable
class ExperienceStore(Protocol):
    async def record(self, record: ExperienceRecord) -> None:
        """Append one run's outcome — the raw material skill distillation
        learns from."""
        ...

    async def load_all(self) -> list[ExperienceRecord]:
        """Every recorded experience; the synthesiser filters, the store
        doesn't."""
        ...


def conversation_messages(run: Run) -> list[dict[str, Any]]:
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
    timestamp: float = field(default_factory=now_wall)
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
    def from_run(cls, run: Run, *, tags: list[str] | None = None) -> ExperienceRecord:
        """Build a record from a completed ``Run``.

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


def _outcome_for(run: Run) -> ExperienceOutcome:
    """Grade a finished run for the learning loop's benefit."""
    if run.state == RunState.COMPLETED:
        output = str(run.final_output or "").strip()
        if not output:
            # "Completed" with nothing to show taught nobody anything.
            return ExperienceOutcome.PARTIAL
        if not run.tool_history:
            # No tools used means no procedure to distil — answering isn't
            # a learnable skill.
            return ExperienceOutcome.PARTIAL
        return ExperienceOutcome.SUCCESS
    if run.state == RunState.FAILED:
        return ExperienceOutcome.FAILURE
    # SUSPENDED / still-running: not a verdict yet.
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
    from prodagent.base.text import tokenize_cjk

    tokens = tokenize_cjk(text)
    seen: dict[str, None] = {}
    for tok in tokens:
        if tok in _STOPWORDS:
            continue
        seen.setdefault(tok, None)
    return list(seen)[:max_tags]
