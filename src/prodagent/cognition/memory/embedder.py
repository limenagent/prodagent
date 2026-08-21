from __future__ import annotations

import hashlib
import struct

from prodagent.core.text import tokenize_cjk
from prodagent.core.vectors import cosine

__all__ = ["HashEmbedder", "cosine"]

_DIM = 256


class HashEmbedder:
    """Bag-of-hashed-tokens pseudo-vectors — swap for a real embedder in production."""

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * _DIM
        for token in tokenize_cjk(text):
            h = hashlib.blake2b(token.encode(), digest_size=4).digest()
            idx = struct.unpack("<I", h)[0] % _DIM
            vec[idx] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0.0:
            vec = [v / norm for v in vec]
        return vec
