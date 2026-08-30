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

from prodagent.kernel.types import LLMResponse
from prodagent.replay.cassette import Cassette, CassetteMismatch, tool_request_hash

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.kernel.types import MessageList
    from prodagent.llm import ChunkCallback, LLMConfig

logger = logging.getLogger(__name__)

__all__ = ["CassetteLLMClient", "CassettePlayer", "cassette_tool"]


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

    ``ok`` returns the recorded value (the dispatcher's coerce throat
    re-wraps it exactly as it wrapped the original). Anything else is a
    lifecycle outcome the engine does not re-enact yet — refused loudly,
    never approximated: a quietly wrong replay is worse than none."""
    outcome = response.get("outcome", "ok")
    if outcome == "ok":
        return response.get("value")
    raise NotImplementedError(
        f"replaying a recorded tool outcome {outcome!r} is not implemented yet — "
        "the ok path is; suspended/handoff lifecycles come with the "
        "equivalence-law unit"
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
