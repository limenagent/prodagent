"""Capability slots — typed provide/require on the bus."""

from __future__ import annotations

from prodagent.kernel.bus import HookRegistry


class _Cap:
    marker: str = "cap"


class _Other:
    pass


def test_provide_require_roundtrip() -> None:
    bus = HookRegistry()
    assert bus.require(_Cap) is None
    impl = _Cap()
    bus.provide(_Cap, impl)
    assert bus.require(_Cap) is impl
    assert bus.require(_Other) is None


def test_provide_overwrites_last_wins() -> None:
    bus = HookRegistry()
    first, second = _Cap(), _Cap()
    bus.provide(_Cap, first)
    bus.provide(_Cap, second)
    assert bus.require(_Cap) is second
