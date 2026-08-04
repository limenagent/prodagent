"""Unified configuration for the framework."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal, cast

_RelationalKind = Literal["file", "postgres"]
_EphemeralKind = Literal["memory", "redis"]


@dataclass
class ContextConfig:
    """L0–L3 context assembly and five-level compression parameters."""

    max_tokens: int = 100_000

    # Should sum to ~1.0
    l0_ratio: float = 0.08
    l1_ratio: float = 0.15
    l2_ratio: float = 0.35
    l3_ratio: float = 0.42

    # Fraction of max_tokens used
    tool_compress_at: float = 0.25
    history_summary_at: float = 0.70
    topic_summary_at: float = 0.85
    emergency_at: float = 0.92

    topic_recent_msgs: int = 4
    history_recent_msgs: int = 6

    spill_tool_results: bool = True
    spill_preview_chars: int = 800

    post_compact_max_tokens_per_skill: int = 5_000
    post_compact_skills_token_budget: int = 25_000

    # Empty string falls back to FrameworkConfig.summary_model.
    summary_model: str = ""
    summary_max_tokens: int = 512
    summary_max_chars_per_turn: int = 800

    safety_margin: int = 500


@dataclass
class LoopConfig:
    """Dead-loop and ghost-loop detection parameters."""

    repeat_threshold: int = 5
    stall_threshold: int = 4
    fingerprint_window: int = 5
    readonly_concurrency: int = 8


@dataclass
class OrchestrationConfig:
    """PLAN_FIRST execution and sub-agent spawning defaults."""

    planning_max_tokens: int = 16_384
    spawn_default_timeout_s: float = 900.0
    spawn_idempotency_ttl_s: float = 600.0
    spawn_handoff_output_max_chars: int = 2000
    spawn_tool_timeout_ms: float = 900_000.0
    spawn_dlq_max_retries: int = 3
    max_peer_chain: int = 5
    events_dir: str = ".prodagent/events"
    runs_dir: str = ".prodagent/runs"
    sessions_dir: str = ".prodagent/sessions"
    spans_path: str = ".prodagent/spans.jsonl"
    experience_path: str = ".prodagent/experiences.jsonl"


@dataclass
class BackendConfig:
    """Pick the backend for each data type — each kind of data goes to the
    store built for it, not one store stretched to cover everything.

    The old ``durable``/``ephemeral`` split was too coarse: it forced graph
    and vector data into whichever KV or relational backend held the rest of
    the state, which is how we ended up with a "Redis graph" that was a JSON
    blob and a "Postgres vector store" that was pgvector. Both are the wrong
    tool. Now each data type has its own field:

    - Relational/durable state (documents, checkpoint, event_log, span) →
      ``file`` (single host) or ``postgres`` (multi-replica).
    - Ephemeral/in-flight state (cache, lock, idempotency, approval,
      dead_letter) → ``memory`` (single host) or ``redis`` (multi-replica).
    - Graph (nodes + edges, traversal) → ``neo4j``. Graphs belong in a graph
      database, period.
    - Vector (embedding ANN) → ``memory`` (local dev) or ``qdrant``
      (production). Vectors belong in a vector database.

    ``*_namespace`` isolate multiple agents (or test runs) sharing one
    instance — every key/row is prefixed.
    """

    # Relational / durable — structured state that survives restarts.
    document: Literal["file", "postgres"] = "file"
    checkpoint: Literal["file", "postgres"] = "file"
    event_log: Literal["file", "postgres"] = "file"
    span: Literal["file", "postgres"] = "file"
    experience: Literal["file"] = "file"
    session: Literal["file", "postgres"] = "file"

    # Ephemeral / in-flight — low-latency coordination state.
    cache: Literal["memory", "redis"] = "memory"
    lock: Literal["memory", "redis"] = "memory"
    idempotency: Literal["memory", "redis"] = "memory"
    approval: Literal["memory", "redis"] = "memory"
    dead_letter: Literal["memory", "redis"] = "memory"

    # Typed stores — each data type has its own dedicated engine.
    graph: Literal["file", "neo4j"] = "file"
    vector: Literal["memory", "qdrant"] = "memory"

    # Connection + isolation.
    redis_namespace: str = "default"
    postgres_namespace: str = "default"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "prodagent"
    vector_dim: int = 1536


@dataclass
class FrameworkConfig:
    """One object to configure all framework-level behaviour."""

    context: ContextConfig = field(default_factory=ContextConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    orchestration: OrchestrationConfig = field(default_factory=OrchestrationConfig)
    backend: BackendConfig = field(default_factory=BackendConfig)
    summary_model: str = ""

    @classmethod
    def default(cls) -> FrameworkConfig:
        return cls()

    @classmethod
    def from_env(cls) -> FrameworkConfig:
        """Build a config from env vars.

        ``PRODAGENT_BACKEND=prod`` flips every backend to its production
        engine (Postgres / Redis / Neo4j / Qdrant); otherwise file + memory
        (the zero-dependency default). Per-backend overrides via
        ``PRODAGENT_BACKEND_<KIND>`` when you want a mix.
        """
        fw = cls.default()
        prod = os.getenv("PRODAGENT_BACKEND", "").lower() == "prod"

        def _env(name: str, default: str) -> str:
            return os.getenv(name, default)

        if prod:
            fw.backend.document = cast(
                "_RelationalKind", _env("PRODAGENT_BACKEND_DOCUMENT", "postgres")
            )
            fw.backend.checkpoint = cast(
                "_RelationalKind", _env("PRODAGENT_BACKEND_CHECKPOINT", "postgres")
            )
            fw.backend.event_log = cast(
                "_RelationalKind", _env("PRODAGENT_BACKEND_EVENT_LOG", "postgres")
            )
            fw.backend.span = cast("_RelationalKind", _env("PRODAGENT_BACKEND_SPAN", "postgres"))
            fw.backend.cache = cast("_EphemeralKind", _env("PRODAGENT_BACKEND_CACHE", "redis"))
            fw.backend.lock = cast("_EphemeralKind", _env("PRODAGENT_BACKEND_LOCK", "redis"))
            fw.backend.idempotency = cast(
                "_EphemeralKind", _env("PRODAGENT_BACKEND_IDEMPOTENCY", "redis")
            )
            fw.backend.approval = cast(
                "_EphemeralKind", _env("PRODAGENT_BACKEND_APPROVAL", "redis")
            )
            fw.backend.dead_letter = cast(
                "_EphemeralKind", _env("PRODAGENT_BACKEND_DEAD_LETTER", "redis")
            )
            fw.backend.graph = cast(
                "Literal['file', 'neo4j']", _env("PRODAGENT_BACKEND_GRAPH", "neo4j")
            )
            fw.backend.vector = cast(
                "Literal['memory', 'qdrant']", _env("PRODAGENT_BACKEND_VECTOR", "qdrant")
            )

        fw.backend.postgres_namespace = _env("PRODAGENT_NAMESPACE", "prodagent")
        fw.backend.redis_namespace = _env("PRODAGENT_NAMESPACE", "prodagent")
        fw.backend.neo4j_uri = _env("NEO4J_URI", fw.backend.neo4j_uri)
        fw.backend.neo4j_user = _env("NEO4J_USER", fw.backend.neo4j_user)
        fw.backend.neo4j_password = _env("NEO4J_PASSWORD", fw.backend.neo4j_password)
        fw.backend.qdrant_url = _env("QDRANT_URL", fw.backend.qdrant_url)
        fw.backend.qdrant_collection = _env("QDRANT_COLLECTION", fw.backend.qdrant_collection)
        run_ns = os.getenv("PRODAGENT_RUN_NAMESPACE")
        if run_ns:
            fw.backend.postgres_namespace = f"{fw.backend.postgres_namespace}-{run_ns}"
            fw.backend.redis_namespace = f"{fw.backend.redis_namespace}-{run_ns}"
        return fw
