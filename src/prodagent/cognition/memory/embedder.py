from __future__ import annotations

import hashlib
import struct
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["HashEmbedder", "cosine"]

_DIM = 256


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return cast("float", dot / ((na * nb) ** 0.5))


class HashEmbedder:
    """Bag-of-hashed-tokens pseudo-vectors — swap for a real embedder in production."""

    def embed(self, text: str) -> list[float]:
        from prodagent.core.text import tokenize_cjk

        vec = [0.0] * _DIM
        for token in tokenize_cjk(text):
            h = hashlib.blake2b(token.encode(), digest_size=4).digest()
            idx = struct.unpack("<I", h)[0] % _DIM
            vec[idx] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0.0:
            vec = [v / norm for v in vec]
        return vec
