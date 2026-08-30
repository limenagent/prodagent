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

__all__ = [
    "CASSETTE_SCHEMA_VERSION",
    "Cassette",
    "CassetteHeader",
    "CassetteMismatch",
    "CassetteRecord",
    "derive_cassette",
    "tool_request_hash",
]
