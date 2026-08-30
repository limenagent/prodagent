"""compose — the assembly root: the only place that reads ``profile``.

``production()`` (core/config.py) flips flags; this module is the consumer
side — the one file that answers "what does a production agent consist of".

Capabilities attach through three sockets, and everything the framework
does uses one of them:

- **Port replacement** — implement a kernel/ports protocol: LLM adapters,
  the caching wrapper, the context assembler, every store backend.
- **Bus attachment** — register on the kernel bus: observers, gates,
  injectors (memory recall, approval veto, spans, learning).
- **Executor replacement** — implement ``LeafExecutor``: PLAN_FIRST is the
  second strategy for iterating the Step atom.

Tools arrive through the hop seam (``tool_assemblers``); capabilities are
found via the bus's typed slots (``provide``/``require``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prodagent.base.config import FrameworkConfig
    from prodagent.coordination.infra.settle import Settler
    from prodagent.coordination.peer import PeerRelay
    from prodagent.hooks.bundles.base import HookBundle
    from prodagent.ports import CheckpointStore, EventLog, SessionStore
    from prodagent.ports.llm import LLMClient
    from prodagent.ports.persistence import BlobStore

logger = logging.getLogger(__name__)

__all__ = [
    "resolve_blob_store",
    "resolve_checkpoint",
    "resolve_event_log",
    "resolve_session_store",
    "wrap_llm",
]


def resolve_blob_store(
    fw: FrameworkConfig, explicit: BlobStore | None, *, event_log: EventLog | None
) -> BlobStore | None:
    """Spill target for oversized boundary facts. Production with an event
    log: the file blob store (big bodies belong on disk). Bare or log-less:
    ``None`` — facts stay inline (bare records nothing anyway)."""
    if explicit is not None:
        return explicit
    if fw.profile != "production" or event_log is None:
        return None
    from prodagent.backends.file.blob import FileBlobStore

    return FileBlobStore(fw.blobs_dir)


def wrap_llm(llm: LLMClient, fw: FrameworkConfig) -> LLMClient:
    """production(): wrap in the response cache. Bare: return as-is — a
    prompt cache is an optimization with observability side effects, not
    part of the loop."""
    if fw.profile != "production":
        return llm
    from prodagent.llm.cache import CachingLLM, CachingLLMClient

    if isinstance(llm, CachingLLM):
        return llm
    return CachingLLMClient(llm, framework_config=fw)


def resolve_checkpoint(
    fw: FrameworkConfig, explicit: CheckpointStore | None
) -> CheckpointStore | None:
    if fw.profile != "production":
        return explicit
    if explicit is not None:
        return explicit
    from prodagent.backends.factory import resolve_checkpoint as _resolve

    return _resolve(fw)


def resolve_event_log(fw: FrameworkConfig, explicit: EventLog | None) -> EventLog | None:
    if fw.profile != "production":
        return explicit
    if explicit is not None:
        return explicit
    from prodagent.backends.factory import resolve_event_log as _resolve

    return _resolve(fw)


def resolve_session_store(fw: FrameworkConfig, explicit: SessionStore | None) -> SessionStore:
    if explicit is not None:
        return explicit
    if fw.profile != "production":
        from prodagent.backends.factory import in_memory_session_store

        return in_memory_session_store()
    from prodagent.backends.factory import resolve_session_store as _resolve

    return _resolve(fw)


def hop_tool_assemblers() -> list[Any]:
    """Collaboration capabilities that contribute hop tools (spawn/peer).

    The driver attaches these to ``RunContext.tool_assemblers``; the factory
    consumes them blind. As the assembly root, this is the one place runtime
    may name coordination capabilities."""
    from prodagent.coordination.infra.stage_tools import assemble_stage_tools
    from prodagent.coordination.peer import assemble_peer_tools
    from prodagent.coordination.spawn import assemble_spawn_tools

    return [
        lambda ctx, tools, schemas, acc: assemble_spawn_tools(ctx, tools, schemas),
        assemble_peer_tools,
        assemble_stage_tools,
    ]


async def find_suspended_peer(checkpoint: Any, root_run_id: str) -> tuple[str, str] | None:
    """Resume discovery — peer chains park their suspended hop in the checkpoint."""
    from prodagent.coordination.peer import find_suspended_peer as _find

    return await _find(checkpoint, root_run_id)


def peer_relay(root_run_id: str) -> PeerRelay:
    """The peer-chain relay — assembled here so runtime names coordination
    in exactly one file (this one)."""
    from prodagent.coordination.peer import PeerRelay as _PeerRelay

    return _PeerRelay(root_run_id)


def make_settler(
    agent_name: str,
    root_run_id: str,
    output_schema: Any,
    output_contract: Any,
) -> Settler:
    """Terminal-state discipline for a finished chain — same seam rule as
    :func:`peer_relay`."""
    from prodagent.coordination.infra.settle import Settler as _Settler

    return _Settler(
        agent_name=agent_name,
        root_run_id=root_run_id,
        output_schema=output_schema,
        output_contract=output_contract,
    )


def default_bundles(fw: FrameworkConfig | None) -> list[HookBundle]:
    """The profile's bundle manifest — what ``attach_default_hooks`` wires.

    The bare profile stays silent: console is opt-in via env/flag, learning
    only attaches when ``skills=`` is set — no observer, no span export, no
    approval gate. The production profile restores the full stack."""
    from prodagent.hooks.bundles.default_wiring import (
        ApprovalDefaultBundle,
        CacheMonitorDefaultBundle,
        ConsoleDefaultBundle,
        LearningDefaultBundle,
        SpanDefaultBundle,
    )

    if fw is None or fw.profile == "bare":
        return [ConsoleDefaultBundle(), LearningDefaultBundle()]
    return [
        ConsoleDefaultBundle(),
        CacheMonitorDefaultBundle(),
        SpanDefaultBundle(),
        ApprovalDefaultBundle(),
        LearningDefaultBundle(),
    ]
