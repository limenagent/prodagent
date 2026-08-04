"""CJK-aware text utilities."""

from __future__ import annotations

import re

__all__ = ["tokenize_cjk", "cjk_char_count"]
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_CJK_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,}")


def tokenize_cjk(text: str, *, min_len: int = 2) -> list[str]:
    if not text:
        return []
    tokens: list[str] = []
    pos = 0
    for m in _CJK_RUN.finditer(text):
        if m.start() > pos:
            for w in _ASCII_WORD.findall(text[pos : m.start()]):
                if len(w) >= min_len:
                    tokens.append(w.lower())
        run = m.group()
        for n in (2, 3):
            if len(run) < n:
                continue
            for i in range(len(run) - n + 1):
                tokens.append(run[i : i + n])
        pos = m.end()
    if pos < len(text):
        for w in _ASCII_WORD.findall(text[pos:]):
            if len(w) >= min_len:
                tokens.append(w.lower())
    return tokens


def cjk_char_count(text: str) -> int:
    return len(_CJK.findall(text))
