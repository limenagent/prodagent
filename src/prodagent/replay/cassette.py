"""Cassette — a run's boundary Q&A as a portable, replayable artifact.

A cassette is a *projection* of the run's boundary stream: every model
answer and every tool result the run received, in order, keyed by the same
request fingerprint the response cache uses. It is the artifact that
travels — copied to a laptop, attached to a bug report, committed to a
test repository — so a production incident can be re-enacted offline with
zero live calls.

Format: JSONL — one JSON object per line, first line the header (schema
version, framework version, run identity, config fingerprint, tool
manifest), then one record per boundary fact (``kind`` = ``llm`` /
``tool``; a ``clock`` kind is reserved for the frozen-clock records the
replay engine will record). Records carry the dual key the matcher pairs
on: position (``seq``) and content fingerprint (``req_hash``).

Serialization follows the house JSONL discipline: ``ensure_ascii=False``,
split on ``"\\n"`` only (never ``splitlines`` — U+0085 and friends are
line breaks to Unicode, not to this format).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from prodagent.base.blobs import fetch_ref
from prodagent.base.event_log import BoundaryEventType, boundary_stream

if TYPE_CHECKING:
    from prodagent.base.event_log import Event
    from prodagent.ports.observability import EventLog
    from prodagent.ports.persistence import BlobStore

logger = logging.getLogger(__name__)

__all__ = [
    "CASSETTE_SCHEMA_VERSION",
    "Cassette",
    "CassetteHeader",
    "CassetteMismatch",
    "CassetteRecord",
    "derive_cassette",
]

CASSETTE_SCHEMA_VERSION = 1


def tool_request_hash(request: dict[str, Any]) -> str:
    """Fingerprint of a tool ask — the ``req_hash`` of ``kind="tool"``
    records. Canonical JSON (sorted keys) under sha256, the same identity
    convention the LLM side's ``cache_key_for`` serves; the matcher and the
    derivation must agree on it, so it lives here, once."""
    from prodagent.base.blobs import digest_of

    return digest_of(json.dumps(request, ensure_ascii=False, sort_keys=True, default=str))


class CassetteMismatch(Exception):
    """The dual-key pairing failed — raised with both sides named, so the
    first divergence reads as "step N requested A, the tape has B", never
    as a vague mismatch."""


@dataclass
class CassetteHeader:
    """Line one of the tape: which machine, which config, which tools."""

    run_id: str
    config_hash: str = ""
    kernel_version: str = ""
    tool_manifest: list[str] = field(default_factory=list)
    schema_version: int = CASSETTE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.kernel_version:
            try:
                from prodagent import __version__

                self.kernel_version = __version__
            except Exception:  # noqa: BLE001 — version is provenance, never load-bearing
                self.kernel_version = "unknown"


@dataclass
class CassetteRecord:
    """One boundary fact: the ask, the answer, and its dual key."""

    seq: int
    """Position on the tape, from 1 — matches the boundary stream's order."""

    kind: str
    """``llm`` or ``tool`` (``clock`` reserved for frozen-clock records)."""

    req_hash: str
    request: dict[str, Any]
    response: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Cassette:
    """A derived, self-contained record of one run's outside world."""

    header: CassetteHeader
    records: list[CassetteRecord] = field(default_factory=list)

    # -- lookup -----------------------------------------------------------

    def find(self, seq: int) -> CassetteRecord | None:
        return self._by_seq().get(seq)

    def match(self, seq: int, req_hash: str) -> CassetteRecord:
        """Dual-key pairing: position AND content fingerprint must agree.

        Position alone would let a stale tape pair with changed code
        silently; the fingerprint alone cannot tell two identical requests
        apart. Together, the first divergence is precisely named."""
        record = self.find(seq)
        if record is None:
            raise CassetteMismatch(
                f"cassette has no record at position {seq} "
                f"(tape holds {len(self.records)})"
            )
        if record.req_hash != req_hash:
            raise CassetteMismatch(
                f"position {seq} requested {req_hash[:12]}… but the tape "
                f"holds {record.req_hash[:12]}… ({record.kind})"
            )
        return record

    def _by_seq(self) -> dict[int, CassetteRecord]:
        return {record.seq: record for record in self.records}

    # -- serialization ------------------------------------------------------

    def to_jsonl(self) -> str:
        lines = [
            json.dumps(
                {
                    "header": True,
                    "schema_version": self.header.schema_version,
                    "kernel_version": self.header.kernel_version,
                    "run_id": self.header.run_id,
                    "config_hash": self.header.config_hash,
                    "tool_manifest": self.header.tool_manifest,
                },
                ensure_ascii=False,
            )
        ]
        for record in self.records:
            lines.append(
                json.dumps(
                    {
                        "seq": record.seq,
                        "kind": record.kind,
                        "req_hash": record.req_hash,
                        "request": record.request,
                        "response": record.response,
                        "meta": record.meta,
                    },
                    ensure_ascii=False,
                )
            )
        return "\n".join(lines) + "\n"

    @classmethod
    def from_jsonl(cls, text: str) -> Cassette:
        """Liberal reader: an unknown future ``schema_version`` is carried,
        not rejected (a tape that loads wrong is recoverable; one that
        refuses to load is not). Lines split on ``\\n`` only."""
        lines = [ln for ln in text.split("\n") if ln.strip()]
        if not lines:
            raise ValueError("cassette is empty")
        head = json.loads(lines[0])
        if not head.get("header"):
            raise ValueError("cassette's first line must be the header record")
        header = CassetteHeader(
            run_id=head.get("run_id", ""),
            config_hash=head.get("config_hash", ""),
            kernel_version=head.get("kernel_version", ""),
            tool_manifest=list(head.get("tool_manifest") or []),
            schema_version=int(head.get("schema_version", CASSETTE_SCHEMA_VERSION)),
        )
        records = [
            CassetteRecord(
                seq=d["seq"],
                kind=d["kind"],
                req_hash=d.get("req_hash", ""),
                request=d.get("request") or {},
                response=d.get("response") or {},
                meta=d.get("meta") or {},
            )
            for d in (json.loads(ln) for ln in lines[1:])
        ]
        return cls(header=header, records=records)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_jsonl(), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> Cassette:
        return cls.from_jsonl(Path(path).read_text(encoding="utf-8"))


async def derive_cassette(
    event_log: EventLog,
    run_id: str,
    *,
    blobs: BlobStore | None = None,
    config_hash: str = "",
    tool_manifest: list[str] | None = None,
) -> Cassette:
    """Project a run's boundary facts into a self-contained cassette.

    Order is the boundary stream's order; ``seq`` renumbers from 1 so the
    tape stands alone. ``$blob`` pointers are resolved back to their bodies
    when a store is given — a cassette that travels carries everything
    inside it. ``tool_manifest`` defaults to the tools the run actually
    used; a caller with the registration roster should pass it."""
    events = await event_log.get_events(boundary_stream(run_id))
    records: list[CassetteRecord] = []
    used_tools: list[str] = []
    for event in events:
        record = await _record_from_fact(event, blobs)
        if record is None:
            continue
        records.append(record)
        if record.kind == "tool":
            name = record.request.get("tool")
            if name and name not in used_tools:
                used_tools.append(name)
    header = CassetteHeader(
        run_id=run_id,
        config_hash=config_hash,
        tool_manifest=tool_manifest if tool_manifest is not None else used_tools,
    )
    for seq, record in enumerate(records, start=1):
        record.seq = seq
    return Cassette(header=header, records=records)


async def _record_from_fact(event: Event, blobs: BlobStore | None) -> CassetteRecord | None:
    data = event.data
    if event.event_type == BoundaryEventType.LLM_RECORDED:
        request = dict(data.get("request") or {})
        response = dict(data.get("response") or {})
        if blobs is not None:
            for field_name in ("messages", "tools"):
                request[field_name] = await fetch_ref(request.get(field_name), blobs)
            if "content" in response:
                response["content"] = await fetch_ref(response.get("content"), blobs)
        return CassetteRecord(
            seq=0,  # renumbered by derive_cassette
            kind="llm",
            req_hash=data.get("req_hash", ""),
            request=request,
            response=response,
            meta={},
        )
    if event.event_type == BoundaryEventType.TOOL_RECORDED:
        request = dict(data.get("request") or {})
        response = dict(data.get("response") or {})
        if blobs is not None and "value" in response:
            response["value"] = await fetch_ref(response.get("value"), blobs)
        meta = dict(data.get("meta") or {})
        return CassetteRecord(
            seq=0,
            kind="tool",
            req_hash=tool_request_hash(request),
            request=request,
            response=response,
            meta=meta,
        )
    return None
