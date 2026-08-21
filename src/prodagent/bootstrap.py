"""Composition root — the only module allowed to import ``prodagent.backends``.

Business code (runtime / coordination / plan) faces ports; when it needs a
default store instance it resolves lazily through here. A module-level
import of ``prodagent.backends`` anywhere outside this file (and the
``__main__`` entry point) fails ``tests/core/test_layering_contract.py``;
loading optional backends on the kernel import chain fails
``tests/core/test_import_weight.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prodagent.core.config import FrameworkConfig
    from prodagent.llm.base import LLMClient, LLMConfig
    from prodagent.ports import (
        CheckpointStore,
        DeadLetterStore,
        EventLog,
        LockStore,
        SessionStore,
    )

__all__ = [
    "resolve_checkpoint",
    "resolve_session_store",
    "resolve_event_log",
    "resolve_dead_letter",
    "resolve_llm",
    "in_process_lock_store",
    "in_memory_dead_letter_queue",
]


def resolve_checkpoint(
    framework_config: FrameworkConfig | None = None,
) -> CheckpointStore:
    from prodagent.backends.factory import resolve_checkpoint as _resolve

    return _resolve(framework_config)


def resolve_session_store(
    framework_config: FrameworkConfig | None = None,
) -> SessionStore:
    from prodagent.backends.factory import resolve_session_store as _resolve

    return _resolve(framework_config)


def resolve_event_log(
    framework_config: FrameworkConfig | None = None,
) -> EventLog:
    from prodagent.backends.factory import resolve_event_log as _resolve

    return _resolve(framework_config)


def resolve_dead_letter(
    framework_config: FrameworkConfig | None = None,
) -> DeadLetterStore:
    from prodagent.backends.factory import resolve_dead_letter as _resolve

    return _resolve(framework_config)


def resolve_llm(
    framework_config: FrameworkConfig | None = None,
    config: LLMConfig | None = None,
) -> LLMClient:
    from prodagent.backends.factory import resolve_llm as _resolve

    return _resolve(framework_config, config)


def in_process_lock_store() -> LockStore:
    """In-process default for primitives that need a lock (single-winner)."""

    from prodagent.backends.memory.lock import InProcessLockStore

    return InProcessLockStore()


def in_memory_dead_letter_queue() -> DeadLetterStore:
    """In-memory default dead-letter mailbox for local development."""

    from prodagent.backends.memory.dead_letter import InMemoryDeadLetterQueue

    return InMemoryDeadLetterQueue()
