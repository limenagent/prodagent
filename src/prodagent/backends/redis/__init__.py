"""Redis-backed implementations of ``prodagent.ports``.

Distributed across replicas: one Redis instance serves many agent processes.
State survives process restarts (Redis persistence) and is visible to every
node. Use when ``FrameworkConfig.backend.durable='redis'`` or
``ephemeral='redis'``.

Requires the ``[redis]`` extra::

    pip install prodagent[redis]

All keys are namespaced under ``prodagent:{namespace}:`` so multiple agents
(or multiple test runs) sharing one Redis do not collide. The default
namespace is ``default``; tests pass a unique namespace per store instance.
"""

from __future__ import annotations

from prodagent.backends.redis.connection import redis_client_from_env
from prodagent.backends.redis.keys import namespaced_key

__all__ = ["redis_client_from_env", "namespaced_key"]
