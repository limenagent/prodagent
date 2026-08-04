"""Redis-backed ``LockStore``."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING

from prodagent.backends.redis.keys import namespaced_key
from prodagent.ports.lock import LockToken

if TYPE_CHECKING:
    from redis.asyncio import Redis

__all__ = ["RedisLockStore"]


_ACQUIRE_POLL_INTERVAL_S = 0.05

# Lua: delete key only if its value matches the token — atomic compare-and-delete
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# Lua: extend TTL only if value matches token
_EXTEND_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
else
    return 0
end
"""


class RedisLockStore:
    """Named distributed lock — token-verified release prevents cross-caller wipe."""

    def __init__(self, client: Redis, *, namespace: str = "default") -> None:
        self._client = client
        self._ns = namespace
        self._release_sha: str | None = None
        self._extend_sha: str | None = None
        self._script_lock = asyncio.Lock()

    def _key(self, name: str) -> str:
        return namespaced_key(self._ns, "lock", name)

    async def _run_script(self, which: str, script: str, keys: int, *args: str | int) -> object:
        """Run a Lua script by SHA, reloading once on NOSCRIPT.

        Redis flushes its script cache on restart (and may evict under memory
        pressure). ``evalsha`` then raises ``NoScriptError`` — without this
        fallback, a Redis restart mid-run breaks every lock release/extend
        until the store is reconstructed.
        """
        sha_attr = "_release_sha" if which == "release" else "_extend_sha"
        async with self._script_lock:
            sha = getattr(self, sha_attr)
            if sha is None:
                sha = await self._client.script_load(script)
                setattr(self, sha_attr, sha)
        try:
            return await self._client.evalsha(sha, keys, *args)
        except Exception as exc:
            from redis.exceptions import NoScriptError

            if not isinstance(exc, NoScriptError):
                raise
            async with self._script_lock:
                sha = await self._client.script_load(script)
                setattr(self, sha_attr, sha)
            return await self._client.evalsha(sha, keys, *args)

    async def acquire(self, name: str, *, timeout: float) -> LockToken:
        token_value = uuid.uuid4().hex
        key = self._key(name)
        deadline = time.monotonic() + timeout

        while True:
            ok = await self._client.set(key, token_value, nx=True, px=int(timeout * 1000))
            if ok:
                return LockToken(name=name, handle=token_value)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"could not acquire lock {name!r} within {timeout}s")
            await asyncio.sleep(_ACQUIRE_POLL_INTERVAL_S)

    async def release(self, token: LockToken) -> None:
        await self._run_script(
            "release", _RELEASE_SCRIPT, 1, self._key(token.name), str(token.handle)
        )

    async def extend(self, token: LockToken, *, ttl: float) -> None:
        await self._run_script(
            "extend", _EXTEND_SCRIPT, 1, self._key(token.name), str(token.handle), int(ttl * 1000)
        )
