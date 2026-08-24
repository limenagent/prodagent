"""Unified configuration for the framework."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal, get_args, get_type_hints


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
    compression: bool = False
    spill_tool_results: bool = False
    spill_preview_chars: int = 800

    inline_compress_min_chars: int = 1_500
    inline_compress_head_chars: int = 500
    inline_compress_tail_chars: int = 200

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
    handoff_idempotency_ttl_s: float = 600.0
    handoff_output_max_chars: int = 2000
    spawn_tool_timeout_ms: float = 900_000.0
    dead_letter_max_retries: int = 3
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

    - Relational/durable state (documents, checkpoint, event_log, span) →
      ``file`` (single host) or ``postgres`` (multi-replica).
    - Ephemeral/in-flight state (cache, lock, approval,
      dead_letter) → ``memory`` (single host) or ``redis`` (multi-replica).
    - Graph (nodes + edges, traversal) → ``neo4j``. Graphs belong in a graph
      database, period.

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
    approval: Literal["memory"] = "memory"
    dead_letter: Literal["memory", "redis"] = "memory"

    # Typed stores — each data type has its own dedicated engine.
    graph: Literal["file", "neo4j"] = "file"

    # Connection + isolation.
    redis_namespace: str = "default"
    postgres_namespace: str = "default"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"


def production(framework_config: FrameworkConfig | None = None) -> FrameworkConfig:
    """One-switch production stack (the counterpart of the bare default).

    Durability (checkpoint/session/event log on file backends), span export,
    the HITL approval gate for HIGH side-effect tools, the LLM response
    cache, and context compression + tool-result spill — everything the bare
    kernel leaves out, restored in one call. The consumer side — exactly
    what these flags attach — is the manifest in ``runtime/compose.py``.
    """
    fw = framework_config or FrameworkConfig.from_env()
    fw.profile = "production"
    fw.context.compression = True
    fw.context.spill_tool_results = True
    return fw


@dataclass
class FrameworkConfig:
    """One object to configure all framework-level behaviour.

    ``profile`` keys every default-resolution site: ``"bare"`` (the default)
    gives the naked kernel — ephemeral in-memory session, no checkpoint /
    event log, no span export, no approval bundle, no LLM cache wrapper, no
    context compression. ``"production"`` restores the full stack; call
    :func:`prodagent.core.config.production` instead of setting it by hand.
    """

    context: ContextConfig = field(default_factory=ContextConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    orchestration: OrchestrationConfig = field(default_factory=OrchestrationConfig)
    backend: BackendConfig = field(default_factory=BackendConfig)
    console_observer: bool = False
    profile: Literal["bare", "production"] = "bare"

    # Declared (not stashed) so dataclasses.replace() carries it over —
    # a replaced config must keep sharing the original's connection pools.
    _backend_registry: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def default(cls) -> FrameworkConfig:
        return cls()

    @classmethod
    def from_env(cls) -> FrameworkConfig:
        """Build a config from env vars.

        ``PRODAGENT_BACKEND=prod`` flips every backend to its production
        engine (Postgres / Redis / Neo4j); otherwise file + memory
        (the zero-dependency default). Per-backend overrides via
        ``PRODAGENT_BACKEND_<KIND>`` when you want a mix.
        """
        fw = cls.default()
        fw.console_observer = os.getenv("PRODAGENT_CONSOLE", "").lower() in (
            "1",
            "true",
            "yes",
        )
        if os.getenv("PRODAGENT_BACKEND", "").lower() == "prod":
            for field_name, prod_default in _PROD_BACKEND_DEFAULTS.items():
                setattr(fw.backend, field_name, prod_default)

        # Per-backend overrides apply in both modes — an explicitly set env var
        # is user intent, not a suggestion to ignore without a word.
        for field_name in _BACKEND_KIND_FIELDS:
            env_name = f"PRODAGENT_BACKEND_{field_name.upper()}"
            raw = os.getenv(env_name)
            if raw is not None:
                _set_backend_kind(fw.backend, field_name, raw)
        _apply_conn_env(fw.backend)
        return fw


def _backend_allowed_values(field_name: str) -> tuple[str, ...]:
    """Legal values for a BackendConfig Literal field, extracted from its annotation."""
    anno = get_type_hints(BackendConfig)[field_name]
    return get_args(anno)


def _set_backend_kind(backend: BackendConfig, field_name: str, raw: str) -> None:
    """Validate + assign a backend-kind env value — fail fast on typos instead
    of deferring the surprise to the first factory lookup."""
    allowed = _backend_allowed_values(field_name)
    if raw not in allowed:
        raise ValueError(
            f"PRODAGENT_BACKEND_{field_name.upper()}={raw!r} is not a valid backend. "
            f"Allowed: {', '.join(allowed)}"
        )
    setattr(backend, field_name, raw)


_BACKEND_KIND_FIELDS: tuple[str, ...] = (
    "document",
    "checkpoint",
    "event_log",
    "span",
    "experience",
    "session",
    "cache",
    "lock",
    "approval",
    "dead_letter",
    "graph",
)

_PROD_BACKEND_DEFAULTS: dict[str, str] = {
    "document": "postgres",
    "checkpoint": "postgres",
    "event_log": "postgres",
    "span": "postgres",
    "cache": "redis",
    "lock": "redis",
    "dead_letter": "redis",
    "graph": "neo4j",
}


def _apply_conn_env(backend: BackendConfig) -> None:
    """Apply connection-string + namespace env vars onto a BackendConfig."""
    ns = os.getenv("PRODAGENT_NAMESPACE", "prodagent")
    backend.postgres_namespace = ns
    backend.redis_namespace = ns
    run_ns = os.getenv("PRODAGENT_RUN_NAMESPACE")
    if run_ns:
        backend.postgres_namespace = f"{ns}-{run_ns}"
        backend.redis_namespace = f"{ns}-{run_ns}"
    backend.neo4j_uri = os.getenv("NEO4J_URI", backend.neo4j_uri)
    backend.neo4j_user = os.getenv("NEO4J_USER", backend.neo4j_user)
    backend.neo4j_password = os.getenv("NEO4J_PASSWORD", backend.neo4j_password)
