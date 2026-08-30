"""The replay engine — the tape answers, the world is substituted.

Re-enacting a recorded run means running the real kernel with a fake
outside: every "ask the model" is answered by the next ``llm`` record on
the tape, every "run this tool" by the next ``tool`` record. Nothing real
is called — the model client IS the tape, and tools are proxies whose
bodies are the recorded answers.

The zero-egress law is structural: a question the tape cannot answer
(no record left, or the request fingerprint disagrees with what the tape
holds at that position) raises :class:`CassetteMismatch` — there is no
fallback to a live call, ever, because a fallback would make every
"successful" replay a possible lie.

Per-kind cursors advance monotonically over the tape: LLM asks walk the
``llm`` records in order, tool asks walk the ``tool`` records. The dual
key still applies within each kind — the next record of the right kind
must also carry the matching fingerprint, so stale tapes pair with
changed code loudly, at the exact position.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.kernel.types import LLMResponse, ToolError, ToolOutcome, ToolResult
from prodagent.replay.cassette import Cassette, CassetteMismatch, tool_request_hash

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.kernel.types import MessageList
    from prodagent.llm import ChunkCallback, LLMConfig

logger = logging.getLogger(__name__)

__all__ = ["CassetteLLMClient", "CassettePlayer", "FrozenClock", "cassette_tool"]


class CassettePlayer:
    """Shared tape position: one monotonic cursor per record kind.

    Both substitutes (the model client and the tool proxies) draw from one
    player, so a run's asks and the tape's answers cannot drift apart even
    when tool batches interleave with model turns."""

    def __init__(self, cassette: Cassette) -> None:
        self._cassette = cassette
        self._by_kind: dict[str, list[Any]] = {}
        for record in cassette.records:
            self._by_kind.setdefault(record.kind, []).append(record)
        self._cursors: dict[str, int] = {}

    @property
    def cassette(self) -> Cassette:
        return self._cassette

    def exhausted(self, kind: str) -> bool:
        return self._cursors.get(kind, 0) >= len(self._by_kind.get(kind, []))

    def answer(self, kind: str, req_hash: str) -> Any:
        """Advance to the next ``kind`` record and demand fingerprint
        consent — the zero-egress choke: no record, no answer, no fallback."""
        records = self._by_kind.get(kind, [])
        index = self._cursors.get(kind, 0)
        position = index + 1
        if index >= len(records):
            raise CassetteMismatch(
                f"the tape has no {kind} record left to answer position "
                f"{position} (asked for {req_hash[:12]}…) — zero egress, no fallback"
            )
        record = records[index]
        if record.req_hash != req_hash:
            raise CassetteMismatch(
                f"position {position} ({kind}) requested {req_hash[:12]}… "
                f"but the tape holds {record.req_hash[:12]}…"
            )
        self._cursors[kind] = index + 1
        return record


class CassetteLLMClient:
    """An ``LLMClient`` whose provider is the tape.

    The request fingerprint is computed exactly as the recorder computed
    it — one ``cache_key_for`` definition spans cache, log, cassette, and
    this matcher — so consent means "the same ask", not "a similar one"."""

    def __init__(self, player: CassettePlayer) -> None:
        self._player = player

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str | list[dict[str, Any]] = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: ChunkCallback | None = None,
    ) -> LLMResponse:
        from prodagent.llm.cache import cache_key_for

        req_hash = cache_key_for(messages, system=system, tools=tools, config=config)
        record = self._player.answer("llm", req_hash)
        return LLMResponse.from_dict(record.response)


_DISPATCHER_INJECTED_KWARGS = frozenset({"run_id"})
"""Keyword arguments the dispatcher adds at invoke time (``fn(**params,
run_id=...)``) — present in a tool function's kwargs but NOT in the
canonical ask the recorder hashed (``call.params``, pre-injection). The
replay side strips them to hash the same object both sides recorded. If
the dispatcher ever injects more, this set and that call site move
together."""


def cassette_tool(name: str, player: CassettePlayer, *, schema: dict[str, Any] | None = None) -> Any:
    """A tool proxy whose execution is the tape's recorded answer.

    The returned object satisfies the dispatcher's FunctionTool shape; its
    ``__call__`` never runs real code — it walks the ``tool`` records and
    settles into the recorded outcome (ok settles its value, error settles
    its error; a recorded suspension is a lifecycle the replay engine does
    not yet re-enact and refuses loudly rather than approximating)."""

    async def _call(**kwargs: Any) -> Any:
        args = {k: v for k, v in kwargs.items() if k not in _DISPATCHER_INJECTED_KWARGS}
        req_hash = tool_request_hash({"tool": name, "args": args})
        record = player.answer("tool", req_hash)
        return _settle(record.response)

    resolved_schema = schema or {
        "name": name,
        "description": f"replayed tool {name}",
        "parameters": {"type": "object", "properties": {}},
    }
    return _ReplayedTool(name=name, call=_call, schema=resolved_schema)


def _settle(response: dict[str, Any]) -> Any:
    """Turn a recorded tool response into what a tool function returns.

    ``ok`` returns the recorded value — the dispatcher's coerce throat
    re-wraps it exactly as it wrapped the original. Every other outcome is
    reconstructed as the ``ToolResult`` it was (the recorded structured
    error, the approval correlation, the handoff descriptor), which coerce
    passes through untouched — so a replayed suspension re-suspends, a
    replayed handoff hands off, a replayed error shows the model the same
    structured feedback."""
    outcome = response.get("outcome", "ok")
    if outcome == "ok":
        return response.get("value")
    detail = response.get("error_detail")
    return ToolResult(
        outcome=ToolOutcome(outcome),
        value=response.get("value"),
        error=ToolError(**detail) if detail else None,
        reason=response.get("reason", ""),
        approval_request_id=response.get("approval_request_id", ""),
        handoff=response.get("handoff"),
    )


class _ReplayedTool:
    """FunctionTool shape over a taped answer — name/meta/schema/call."""

    def __init__(self, *, name: str, call: Callable[..., Any], schema: dict[str, Any]) -> None:
        from prodagent.kernel.types import SideEffectLevel, ToolMeta

        self.name = name
        self._call = call
        self.schema = schema
        self.meta = ToolMeta(
            name=name,
            # A replay is a read of the past: the recorded run already paid
            # for its side effects, and the proxy executes none.
            is_readonly=True,
            side_effect_level=SideEffectLevel.LOW,
        )

    async def __call__(self, **kwargs: Any) -> Any:
        return await self._call(**kwargs)


def replay_tools(cassette: Cassette, player: CassettePlayer) -> dict[str, Any]:
    """Tool map for a replayed run: one proxy per tool the tape mentions."""
    names: list[str] = []
    for record in cassette.records:
        if record.kind == "tool":
            name = record.request.get("tool")
            if name and name not in names:
                names.append(name)
    return {name: cassette_tool(name, player) for name in names}


class FrozenClock:
    """A ``TimePort`` whose readings come from the tape — never the wall.

    Wall and monotonic readings each replay their recorded sequence, in
    order. Past the tape's end the clock CLAMPS to the last recorded
    reading: a replay that asks more clock questions than its recording did
    still stays offline-deterministic (a frozen clock advancing would be a
    lie), and any decision that genuinely diverged is caught by the
    comparator, not by the clock."""

    def __init__(self, cassette: Cassette) -> None:
        self._readings: dict[str, list[float]] = {"wall": [], "monotonic": []}
        self._cursors: dict[str, int] = {"wall": 0, "monotonic": 0}
        for record in cassette.records:
            if record.kind == "clock":
                port_name = record.response.get("port")
                if port_name in self._readings:
                    self._readings[port_name].append(record.response.get("value", 0.0))

    def _next(self, port_name: str) -> float:
        readings = self._readings[port_name]
        index = self._cursors[port_name]
        if index < len(readings):
            self._cursors[port_name] = index + 1
            return readings[index]
        return readings[-1] if readings else 0.0  # clamp: frozen means frozen

    def wall(self) -> float:
        return self._next("wall")

    def monotonic(self) -> float:
        return self._next("monotonic")
