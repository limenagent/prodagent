"""Determinism ports — defaults, substitution, and context isolation.

These tests pin the three properties the Replay laws stand on:

1. installing nothing behaves exactly like the stdlib sources (the migration
   was a no-op by construction — prove it);
2. ``override`` swaps every port and restores it on the way out, exception
   or not (a crashed replay must not leak a frozen clock);
3. contextvar isolation scopes a swap to one async task — sibling tasks
   keep the real clock.

Plus the migration canary: the worst historical leak — ``Event.make``
stamping event ids and timestamps straight from uuid4/time.time — now
honours the ports.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from prodagent.base.determinism import (
    new_uuid4,
    now_monotonic,
    now_wall,
    override,
    random_uniform,
)


class FrozenTime:
    """Fixed clock — what a replay's RecordedClock reduces to."""

    def __init__(self, wall: float, mono: float) -> None:
        self._wall = wall
        self._mono = mono

    def wall(self) -> float:
        return self._wall

    def monotonic(self) -> float:
        return self._mono


class FixedRandom:
    def __init__(self, value: float) -> None:
        self._value = value

    def uniform(self, lo: float, hi: float) -> float:
        return self._value


class FixedIds:
    def __init__(self, value: str) -> None:
        self._value = value

    def uuid4(self) -> str:
        return self._value


def test_defaults_delegate_to_real_sources() -> None:
    before = time.time()
    w = now_wall()
    after = time.time()
    assert before <= w <= after
    # A real uuid4 string, not a placeholder.
    uuid.UUID(new_uuid4())
    assert now_monotonic() <= now_monotonic()
    assert 0.0 <= random_uniform(0.0, 1.0) <= 1.0


def test_override_swaps_all_three_ports_and_restores() -> None:
    with override(
        time_port=FrozenTime(42.0, 7.0),
        random_port=FixedRandom(0.5),
        id_port=FixedIds("fixed-id"),
    ):
        assert now_wall() == 42.0
        assert now_monotonic() == 7.0
        assert random_uniform(0.0, 10.0) == 0.5
        assert new_uuid4() == "fixed-id"
    # Restored to the real clock (epoch seconds, never 42.0).
    assert abs(now_wall() - time.time()) < 5.0
    uuid.UUID(new_uuid4())


def test_override_resets_on_exception() -> None:
    class Boom(Exception):
        pass

    try:
        with override(time_port=FrozenTime(42.0, 7.0)):
            raise Boom
    except Boom:
        pass
    assert abs(now_wall() - time.time()) < 5.0


def test_override_is_per_async_task() -> None:
    """Contextvars copy per task: a frozen clock in one task must not leak
    into a sibling — the property that lets a replay run beside live work."""

    async def scenario() -> list[float]:
        seen: list[float] = []

        async def frozen_task() -> None:
            with override(time_port=FrozenTime(42.0, 7.0)):
                await asyncio.sleep(0)
                seen.append(now_wall())

        async def live_task() -> None:
            await asyncio.sleep(0)
            seen.append(now_wall())

        await asyncio.gather(frozen_task(), live_task())
        return seen

    seen = asyncio.run(scenario())
    assert seen[0] == 42.0
    assert abs(seen[1] - time.time()) < 5.0


def test_event_make_honours_ports() -> None:
    """Migration canary: the worst leak (event ids/timestamps straight from
    uuid4/time.time into the durable event stream) is ported — replay can
    now re-mint recorded events bit-for-bit."""
    from prodagent.base.event_log import Event

    with override(time_port=FrozenTime(100.0, 0.0), id_port=FixedIds("ev-fixed")):
        e = Event.make("NodeCompleted", "stream-1", 3, foo="bar")
    assert e.event_id == "ev-fixed"
    assert e.timestamp == 100.0
    assert e.data == {"foo": "bar"}


def test_retry_jitter_honours_port() -> None:
    """The only in-domain randomness becomes deterministic under a fixed
    port — retry timing joins the replayable world."""
    from prodagent.base.retry import Backoff, RetryPolicy

    policy = RetryPolicy(base_delay=1.0, max_delay=60.0, backoff=Backoff.JITTERED)
    with override(random_port=FixedRandom(0.25)):
        assert policy.delay(attempt=2) == 0.25
