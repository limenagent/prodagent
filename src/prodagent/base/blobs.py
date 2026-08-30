"""Content-addressed spill — the pointer half of oversized boundary facts.

A boundary fact that exceeds the configured threshold must not be dropped
(replay needs the whole answer) and must not ride the hot log line (every
append re-reads the tail). The resolution is pointer-style truncation: the
body goes to a ``BlobStore`` under its sha256 digest, and the log record
carries ``{"$blob": digest, "$size": n}`` — small line, recoverable whole.

The ``$``-prefixed marker keys are reserved: a payload carrying them IS a
reference, never user data that happens to collide (a tool returning a
dict with a ``$blob`` key of its own is recorded as an ordinary value —
the marker only carries meaning at the field boundary the recorders
create, and ``fetch_ref`` is its only interpreter).
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prodagent.ports.persistence import BlobStore

__all__ = [
    "BLOB_REF_KEY",
    "DEFAULT_THRESHOLD_BYTES",
    "SIZE_REF_KEY",
    "digest_of",
    "fetch_ref",
    "spill_value",
]

BLOB_REF_KEY = "$blob"
SIZE_REF_KEY = "$size"

DEFAULT_THRESHOLD_BYTES = 65_536
"""Facts at or under this size stay inline; bigger ones spill. 64KB keeps a
boundary line readable in an editor while keeping the hot tail small."""


def digest_of(text: str) -> str:
    """sha256 hex digest of the utf-8 body — the one identity every
    content-addressed consumer (log pointer, span pointer, cassette
    pointer) shares."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _serialized_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(repr(value))


async def spill_value(
    value: Any,
    blobs: BlobStore,
    *,
    threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
) -> Any:
    """Return ``value`` inline, or a ``{"$blob": digest, "$size": n}``
    reference when its serialized form exceeds ``threshold_bytes``.

    The body is stored as canonical JSON so any JSON-shaped value round
    trips through :func:`fetch_ref` unchanged."""
    if _serialized_size(value) <= threshold_bytes:
        return value
    body = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    digest = await blobs.put(body)
    return {BLOB_REF_KEY: digest, SIZE_REF_KEY: len(body)}


def _looks_like_ref(value: Any) -> bool:
    """A reference, by shape: exactly the marker keys, and the digest is a
    64-char hex string. The shape check is what makes the ``$`` namespace
    safe — a tool returning ``{"$blob": "not-a-digest"}`` as user data is an
    ordinary value, not a pointer."""
    if not isinstance(value, dict) or BLOB_REF_KEY not in value:
        return False
    if not set(value) <= {BLOB_REF_KEY, SIZE_REF_KEY}:
        return False
    digest = value[BLOB_REF_KEY]
    return (
        isinstance(digest, str)
        and len(digest) == 64
        and all(c in "0123456789abcdef" for c in digest)
    )


async def fetch_ref(ref: Any, blobs: BlobStore) -> Any:
    """Resolve what :func:`spill_value` produced — a ref becomes its original
    value (JSON-decoded back to life); anything else passes through
    untouched, so callers can feed whole event payloads through this."""
    if _looks_like_ref(ref):
        body = await blobs.get(ref[BLOB_REF_KEY])
        if body is None:
            raise KeyError(f"blob body {ref[BLOB_REF_KEY]!r} is missing from the store")
        return json.loads(body)
    return ref
