"""CJK-aware text utilities."""

from __future__ import annotations

import re

__all__ = ["tokenize_cjk", "cjk_char_count", "bound_text"]
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_CJK_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,}")


def tokenize_cjk(text: str, *, min_len: int = 2) -> list[str]:
    """Tokenize mixed CJK/ASCII text for lexical matching."""
    # CJK text has no word boundaries; character n-grams (2..3) buy recall
    # without shipping a segmenter — precision is the embedder's job.
    if not text:
        return []
    tokens: list[str] = []
    pos = 0
    for m in _CJK_RUN.finditer(text):
        if m.start() > pos:
            # ASCII words between CJK runs tokenize the normal way.
            for w in _ASCII_WORD.findall(text[pos : m.start()]):
                if len(w) >= min_len:
                    tokens.append(w.lower())
        run = m.group()
        # Each contiguous CJK run yields its 2-grams and 3-grams — recall
        # without a segmenter; precision is the embedder's job.
        for n in (2, 3):
            if len(run) < n:
                continue
            for i in range(len(run) - n + 1):
                tokens.append(run[i : i + n])
        pos = m.end()
    if pos < len(text):
        # Trailing ASCII tail after the last CJK run.
        for w in _ASCII_WORD.findall(text[pos:]):
            if len(w) >= min_len:
                tokens.append(w.lower())
    return tokens


def cjk_char_count(text: str) -> int:
    return len(_CJK.findall(text))


def bound_text(text: str, max_chars: int) -> str:
    """Clip *text* to *max_chars*, noting how much was cut."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n…(truncated, {len(text) - max_chars} more chars)"
