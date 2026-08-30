"""RecordingLLMClient — every LLM answer lands on the run's boundary stream.

The LLM half of the unified fact pipeline: one wrapper
over the shared client catches every mode's calls — REACTIVE's Step,
PLAN_FIRST's planner and step runner, Workflow — because they all drive the
same wired client. Facts append to ``<run_id>#boundary`` (a sibling stream,
so the marker stream's single-writer discipline is untouched), keyed by the
same ``cache_key_for`` fingerprint the response cache uses — one hash
identity for cache hits, log lookup, and future cassette derivation.

Calls made outside any run scope (background skill distillation) are
skipped: they are not facts of a run.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from prodagent.base.blobs import DEFAULT_THRESHOLD_BYTES, spill_value
from prodagent.base.event_log import BoundaryEventType, Event, boundary_stream
from prodagent.base.run_context import current_run_id
from prodagent.llm.cache import cache_key_for

if TYPE_CHECKING:
    from prodagent.kernel.types import LLMResponse, MessageList
    from prodagent.llm import ChunkCallback, LLMConfig
    from prodagent.ports.llm import LLMClient
    from prodagent.ports.observability import EventLog
    from prodagent.ports.persistence import BlobStore

logger = logging.getLogger(__name__)

__all__ = ["RecordingLLM", "RecordingLLMClient"]


@runtime_checkable
class RecordingLLM(Protocol):
    """Marker protocol for an LLM client that records boundary facts."""

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str | list[dict[str, Any]] = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: ChunkCallback | None = None,
    ) -> LLMResponse: ...

    def unwrap(self) -> Any: ...


class RecordingLLMClient:
    """Wrap an ``LLMClient``; record each answered call on the boundary stream.

    Oversized fields (messages, tool schemas, the response) spill to
    ``blobs`` as ``{"$blob": digest}`` pointers when one is configured —
    pointer-style truncation, never dropped bytes."""

    def __init__(
        self,
        inner: LLMClient,
        event_log: EventLog,
        *,
        blobs: BlobStore | None = None,
        threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
    ) -> None:
        self._inner = inner
        self._event_log = event_log
        self._blobs = blobs
        self._threshold = threshold_bytes

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str | list[dict[str, Any]] = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: ChunkCallback | None = None,
    ) -> LLMResponse:
        response = await self._inner.complete(
            messages, system=system, tools=tools, config=config, on_chunk=on_chunk
        )
        run_id = current_run_id()
        if run_id is None:
            # Outside any run — background work, not a fact of a run.
            logger.debug("[boundary] skipping off-run LLM call (%d msgs)", len(messages))
            return response
        try:
            recorded_response = dict(response.to_dict())
            if self._blobs is not None:
                recorded_response["content"] = await spill_value(
                    recorded_response.get("content"), self._blobs,
                    threshold_bytes=self._threshold,
                )
            await self._event_log.append(
                Event.make(
                    BoundaryEventType.LLM_RECORDED,
                    stream_id=boundary_stream(run_id),
                    version=0,
                    req_hash=cache_key_for(messages, system=system, tools=tools, config=config),
                    request={
                        "messages": (
                            await spill_value(list(messages), self._blobs,
                                             threshold_bytes=self._threshold)
                            if self._blobs is not None
                            else list(messages)
                        ),
                        "system": system,
                        "tools": (
                            await spill_value(tools or [], self._blobs,
                                              threshold_bytes=self._threshold)
                            if self._blobs is not None
                            else tools or []
                        ),
                        "config": dataclasses.asdict(config) if config is not None else None,
                    },
                    response=recorded_response,
                )
            )
        except Exception:  # noqa: BLE001 — recording must never kill the run
            logger.exception("[boundary] failed to record LLM call for %s", run_id)
        return response

    def unwrap(self) -> Any:
        return self._inner
