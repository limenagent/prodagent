"""Redis client construction — single place that reads connection env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, overload

if TYPE_CHECKING:
    from redis import Redis
    from redis.asyncio import Redis as AsyncRedis


@dataclass(frozen=True)
class _TcpConn:
    host: str
    port: int
    db: int
    decode_responses: bool = False


def _url() -> str | None:
    return os.getenv("REDIS_URL") or None


def _tcp_conn() -> _TcpConn:
    return _TcpConn(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
    )


@overload
def redis_client_from_env(*, async_: Literal[False] = False) -> Redis: ...
@overload
def redis_client_from_env(*, async_: Literal[True]) -> AsyncRedis: ...


def redis_client_from_env(*, async_: bool = False) -> Redis | AsyncRedis:
    """Build a Redis client from ``REDIS_URL`` or ``REDIS_HOST``/``REDIS_PORT``.

    ``async_=True`` returns ``redis.asyncio.Redis``; otherwise the sync ``Redis``.
    """
    url = _url()
    tcp = _tcp_conn()
    if async_:
        from redis.asyncio import Redis as AsyncRedis

        if url is not None:
            return AsyncRedis.from_url(url, decode_responses=False)
        return AsyncRedis(
            host=tcp.host,
            port=tcp.port,
            db=tcp.db,
            decode_responses=False,
        )
    from redis import Redis as SyncRedis

    if url is not None:
        return SyncRedis.from_url(url, decode_responses=False)
    return SyncRedis(
        host=tcp.host,
        port=tcp.port,
        db=tcp.db,
        decode_responses=False,
    )
