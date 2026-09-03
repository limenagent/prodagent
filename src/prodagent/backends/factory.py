"""factory — the dispatch desk turning ``FrameworkConfig`` into backends.

Every port has a ``resolve_xxx()`` here, and none of them branches on
implementation: the mapping lives as *data* (``_BACKENDS``, one row per
port per medium — import path, constructor args pulled from config, kwargs),
so adding a backend is a table entry, not a new if-chain. Driver imports
happen inside ``_resolve`` at selection time — an unselected backend's SDK
is never imported, which is what keeps optional dependencies optional."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from prodagent.ports import (
    ApprovalStore,
    CacheStore,
    CheckpointStore,
    DocumentStore,
    EventLog,
    ExperienceStore,
    GraphStore,
    LockStore,
    SessionStore,
    SpanExporter,
)

if TYPE_CHECKING:
    from prodagent.base.config import FrameworkConfig
    from prodagent.llm import LLMConfig
    from prodagent.ports.llm import LLMClient


def resolve_aux_llm(
    framework_config: FrameworkConfig | None = None,
    *,
    offline_content: str = "{}",
) -> LLMClient:
    """Aux LLM for background helpers (memory classify/conflict, skill synthesis).

    Same provider resolution as the main LLM, but offline/no-key setups get a
    scripted single-response adapter instead of the echo fake — the default
    ``{}`` is the no-op answer for classify/conflict JSON prompts.
    """
    from prodagent.llm.fake import script
    from prodagent.llm.providers import anthropic_env, openai_compat_env, use_fake_llm

    if use_fake_llm() or (not openai_compat_env() and not anthropic_env()):
        return script({"content": offline_content})
    return resolve_llm(framework_config)


def in_process_lock_store() -> LockStore:
    """In-process default for primitives that need a lock (single-winner)."""

    from prodagent.backends.memory.lock import InProcessLockStore

    return InProcessLockStore()


def in_memory_session_store() -> SessionStore:
    """In-process session store — the bare profile's default (no disk)."""

    from prodagent.backends.memory.session_store import InMemorySessionStore

    return InMemorySessionStore()


def in_memory_checkpoint_store() -> CheckpointStore:
    """In-process checkpoint store — PLAN_FIRST's bare-profile default (no disk)."""

    from prodagent.backends.memory.checkpoint import InMemoryCheckpointStore

    return InMemoryCheckpointStore()


def in_memory_event_log() -> EventLog:
    """In-process event log — PLAN_FIRST's bare-profile default (no disk)."""

    from prodagent.backends.memory.event_log import InMemoryEventLog

    return InMemoryEventLog()


__all__ = [
    "resolve_llm",
    "resolve_checkpoint",
    "resolve_session_store",
    "resolve_event_log",
    "resolve_cache",
    "resolve_approval",
    "resolve_lock",
    "resolve_span_exporter",
    "resolve_document",
    "resolve_graph",
    "resolve_experience",
    "resolve_aux_llm",
    "in_process_lock_store",
    "in_memory_session_store",
    "in_memory_checkpoint_store",
    "in_memory_event_log",
]


def _fw(framework_config: FrameworkConfig | None) -> FrameworkConfig:
    from prodagent.base.config import FrameworkConfig

    return framework_config or FrameworkConfig.default()


Ctx = Any  # {"fw": fw, "reg": reg}
Get = Callable[[Ctx], Any]
Spec = tuple[str, list[Get], dict[str, Get]]


def _b(name: str) -> Get:
    return lambda c: getattr(c["fw"].backend, name)


def _o(name: str) -> Get:
    return lambda c: getattr(c["fw"].orchestration, name)


def _r(method: str) -> Get:
    return lambda c: getattr(c["reg"], method)()


_BACKENDS: dict[str, dict[str, Spec]] = {
    # port → kind → ("module:Class", [args from ctx], {kwargs from ctx}).
    # ctx carries the framework config and the shared-client registry, so a
    # postgres store constructor receives the *pooled* connection, not a
    # fresh one — pooling policy stays in registry.py, one home.
    "checkpoint": {
        "file": ("prodagent.backends.file.checkpoint:FileCheckpointStore", [_o("runs_dir")], {}),
        "postgres": (
            "prodagent.backends.postgres.checkpoint:PostgresCheckpointStore",
            [_r("pg_async_pool")],
            {"namespace": _b("postgres_namespace")},
        ),
    },
    "session": {
        "file": (
            "prodagent.backends.file.session_store:FileSessionStore",
            [_o("sessions_dir")],
            {},
        ),
        "postgres": (
            "prodagent.backends.postgres.session_store:PostgresSessionStore",
            [_r("pg_async_pool")],
            {"namespace": _b("postgres_namespace")},
        ),
    },
    "event_log": {
        "file": ("prodagent.backends.file.event_log:FileEventLog", [_o("events_dir")], {}),
        "postgres": (
            "prodagent.backends.postgres.event_log:PostgresEventLog",
            [_r("pg_async_pool")],
            {"namespace": _b("postgres_namespace")},
        ),
    },
    "span": {
        "file": ("prodagent.backends.file.span:FileSpanExporter", [_o("spans_path")], {}),
        "postgres": (
            "prodagent.backends.postgres.span:PostgresSpanExporter",
            [_r("pg_sync_pool")],
            {"namespace": _b("postgres_namespace")},
        ),
    },
    "document": {
        "file": ("prodagent.backends.file.document:FileDocumentStore", [_o("runs_dir")], {}),
        "postgres": (
            "prodagent.backends.postgres.document:PostgresDocumentStore",
            [_r("pg_sync_pool")],
            {"namespace": _b("postgres_namespace")},
        ),
    },
    "cache": {
        "memory": ("prodagent.backends.memory.cache:InMemoryCache", [], {}),
        "redis": (
            "prodagent.backends.redis.cache:RedisCache",
            [_r("redis_async_client")],
            {"namespace": _b("redis_namespace")},
        ),
    },
    "approval": {
        "memory": ("prodagent.backends.memory.approval:InMemoryApprovalStore", [], {}),
    },
    "lock": {
        "memory": ("prodagent.backends.memory.lock:InProcessLockStore", [], {}),
        "redis": (
            "prodagent.backends.redis.lock:RedisLockStore",
            [_r("redis_async_client")],
            {"namespace": _b("redis_namespace")},
        ),
    },
    "graph": {
        "file": ("prodagent.backends.file.graph:FileGraphStore", [_o("runs_dir")], {}),
        "neo4j": (
            "prodagent.backends.neo4j.graph:Neo4jGraphStore",
            [],
            {"uri": _b("neo4j_uri"), "user": _b("neo4j_user"), "password": _b("neo4j_password")},
        ),
    },
    "experience": {
        "file": (
            "prodagent.backends.file.experience:FileExperienceStore",
            [_o("experience_path")],
            {},
        ),
    },
}


def _resolve(port: str, framework_config: FrameworkConfig | None, *, expect: type[Any]) -> Any:
    """One resolver for every port: read the configured kind, look up its
    spec row, import the class (function-body import — optional deps), build
    with config-sourced args, and structurally verify the result actually
    satisfies the port before handing it back."""
    fw = _fw(framework_config)
    kind = getattr(fw.backend, port)
    spec = _BACKENDS.get(port, {}).get(kind)
    if spec is None:
        raise NotImplementedError(f"{port} backend {kind!r} not implemented yet")
    import_path, args, kwargs = spec
    module_name, _, class_name = import_path.partition(":")

    from prodagent.backends.registry import BackendRegistry

    ctx = {
        "fw": fw,
        "reg": BackendRegistry.for_config(fw),
    }  # pooled clients flow in, not fresh ones
    cls = getattr(
        importlib.import_module(module_name), class_name
    )  # the optional-dependency moment
    result = cls(*[a(ctx) for a in args], **{k: v(ctx) for k, v in kwargs.items()})
    if not isinstance(result, expect):
        raise TypeError(
            f"Backend loader for {port!r} returned {type(result).__name__}, "
            f"expected {expect.__name__}"
        )
    return result


def resolve_checkpoint(framework_config: FrameworkConfig | None = None) -> CheckpointStore:
    """Checkpoint store per ``fw.backend.checkpoint`` (file default)."""
    return cast("CheckpointStore", _resolve("checkpoint", framework_config, expect=CheckpointStore))


def resolve_session_store(framework_config: FrameworkConfig | None = None) -> SessionStore:
    """Session store per ``fw.backend.session`` (file default)."""
    return cast("SessionStore", _resolve("session", framework_config, expect=SessionStore))


def resolve_event_log(framework_config: FrameworkConfig | None = None) -> EventLog:
    """Event log per ``fw.backend.event_log`` (file default)."""
    return cast("EventLog", _resolve("event_log", framework_config, expect=EventLog))


def resolve_span_exporter(framework_config: FrameworkConfig | None = None) -> SpanExporter:
    """Span exporter per ``fw.backend.span`` (file default)."""
    return cast("SpanExporter", _resolve("span", framework_config, expect=SpanExporter))


def resolve_document(framework_config: FrameworkConfig | None = None) -> DocumentStore:
    """Memory-document store per ``fw.backend.document`` (file default)."""
    return cast("DocumentStore", _resolve("document", framework_config, expect=DocumentStore))


def resolve_cache(framework_config: FrameworkConfig | None = None) -> CacheStore:
    """LLM response cache per ``fw.backend.cache`` (memory default)."""
    return cast("CacheStore", _resolve("cache", framework_config, expect=CacheStore))


def resolve_approval(framework_config: FrameworkConfig | None = None) -> ApprovalStore:
    """Approval store per ``fw.backend.approval`` (memory default)."""
    return cast("ApprovalStore", _resolve("approval", framework_config, expect=ApprovalStore))


def resolve_lock(framework_config: FrameworkConfig | None = None) -> LockStore:
    """Lock store per ``fw.backend.lock`` (memory default)."""
    return cast("LockStore", _resolve("lock", framework_config, expect=LockStore))


def resolve_graph(framework_config: FrameworkConfig | None = None) -> GraphStore:
    """Graph store per ``fw.backend.graph`` (file default, neo4j for graphs)."""
    return cast("GraphStore", _resolve("graph", framework_config, expect=GraphStore))


def resolve_experience(framework_config: FrameworkConfig | None = None) -> ExperienceStore:
    """Experience store per ``fw.backend.experience`` (JSONL file)."""
    return cast("ExperienceStore", _resolve("experience", framework_config, expect=ExperienceStore))


# --- LLM: env-driven, no registry table ---


def resolve_llm(
    framework_config: FrameworkConfig | None = None,  # noqa: ARG001 — symmetry with other resolvers
    config: LLMConfig | None = None,
) -> LLMClient:
    """LLM adapter — picked from env (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``
    / compat presets / ``USE_FAKE_LLM``). The ``framework_config`` argument is
    accepted for symmetry with the other resolvers; provider selection today
    is env-only, so it's unused. ``config`` lets the caller override the model
    name (e.g. judge LLM) without going through env.

    Examples never call this directly — ``RunContext.__aenter__`` resolves
    the LLM via ``framework_config`` when the agent's ``llm`` is ``None``.
    """
    from prodagent.llm.factory import create_llm_client

    return create_llm_client(config)
