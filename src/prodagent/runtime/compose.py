"""compose — the only place that reads ``profile``.

``production()`` (core/config.py) flips flags; this module is the consumer
side: what those flags actually attach. Bare profile resolves nothing —
explicit config still works, ``None`` stays ``None``, nothing touches disk.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prodagent.core.config import FrameworkConfig
    from prodagent.ports import CheckpointStore, EventLog, SessionStore
    from prodagent.ports.llm import LLMClient

logger = logging.getLogger(__name__)

__all__ = ["wrap_llm", "resolve_checkpoint", "resolve_event_log", "resolve_session_store"]


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


def resolve_checkpoint(fw: FrameworkConfig, explicit: CheckpointStore | None) -> CheckpointStore | None:
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
