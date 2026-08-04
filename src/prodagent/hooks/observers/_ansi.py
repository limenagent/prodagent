"""ANSI colour helpers — stdlib only."""

from __future__ import annotations

import sys

_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_BLUE = "\033[94m"
_CYAN = "\033[96m"
_MAGENTA = "\033[95m"
_RESET = "\033[0m"


def colorize(text: str, *codes: str) -> str:
    if not sys.stdout.isatty():
        return text
    return "".join(codes) + text + _RESET


__all__ = [
    "_BOLD",
    "_DIM",
    "_GREEN",
    "_YELLOW",
    "_RED",
    "_BLUE",
    "_CYAN",
    "_MAGENTA",
    "_RESET",
    "colorize",
]
