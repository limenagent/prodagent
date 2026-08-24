"""RedisLockStore trylock semantics — stub client, no Redis required."""

from __future__ import annotations

import pytest

from prodagent.backends.redis.lock import RedisLockStore


class _StubClient:
    def __init__(self) -> None:
        self.held: set[str] = set()
        self.calls: list[dict] = []

    async def set(self, key, value, *, nx=False, px=None):
        self.calls.append({"key": key, "nx": nx, "px": px})
        if nx and key in self.held:
            return None
        self.held.add(key)
        return True


async def test_nonblocking_acquire_on_free_lock() -> None:
    client = _StubClient()
    store = RedisLockStore(client)
    token = await store.acquire("res", timeout=0)
    assert token.name == "res"
    assert len(client.calls) == 1  # a trylock is a single attempt


async def test_nonblocking_acquire_on_held_lock_raises() -> None:
    store = RedisLockStore(_StubClient())
    await store.acquire("res", timeout=0)
    with pytest.raises(TimeoutError):
        await store.acquire("res", timeout=0)


@pytest.mark.parametrize("timeout", [0, 0.0001, -1.0])
async def test_px_is_always_positive(timeout: float) -> None:
    client = _StubClient()
    store = RedisLockStore(client)
    await store.acquire("res", timeout=timeout)
    assert client.calls, "free lock must succeed on the first attempt"
    assert all(c["px"] >= 1 for c in client.calls), "px<=0 is a Redis protocol error"
