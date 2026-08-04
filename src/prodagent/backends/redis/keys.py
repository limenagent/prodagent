"""Key namespacing — every Redis key is prefixed to avoid collisions."""

from __future__ import annotations


def namespaced_key(namespace: str, *parts: str) -> str:
    """Build ``prodagent:{namespace}:{part1}:{part2}:...`` from parts."""
    cleaned = [p for p in parts if p]
    return ":".join(["prodagent", namespace, *cleaned])
