"""Replay — the runtime's recorded-past machinery.

A run's boundary Q&A is a fact stream on the WAL; this package turns that
past into an artifact (the cassette) and, in stages, into re-enactment:
offline replay of a recorded run, equivalence checking against the
original, and rollback of already-happened effects.
"""

from prodagent.replay.cassette import (
    CASSETTE_SCHEMA_VERSION,
    Cassette,
    CassetteHeader,
    CassetteMismatch,
    CassetteRecord,
    derive_cassette,
    tool_request_hash,
)
from prodagent.replay.engine import CassetteLLMClient, CassettePlayer, FrozenClock, replay_tools
from prodagent.replay.strict import (
    ReplayNotEquivalent,
    assert_equivalent,
    event_flow_projection,
    strict_compare,
    terminal_projection,
)

__all__ = [
    "CASSETTE_SCHEMA_VERSION",
    "Cassette",
    "CassetteHeader",
    "CassetteLLMClient",
    "CassetteMismatch",
    "CassettePlayer",
    "CassetteRecord",
    "ReplayNotEquivalent",
    "assert_equivalent",
    "derive_cassette",
    "FrozenClock",
    "event_flow_projection",
    "replay_tools",
    "strict_compare",
    "terminal_projection",
    "tool_request_hash",
]
