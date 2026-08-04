"""In-process, ephemeral backend implementations.

Default for ephemeral state (approval, idempotency, locks, dead-letter, LLM
response cache). State dies with the process — fine for single-host runs and
tests. For multi-replica deployments, swap in ``prodagent.backends.redis``.
"""

from prodagent.backends.memory.approval import InMemoryApprovalStore
from prodagent.backends.memory.cache import InMemoryCache
from prodagent.backends.memory.dead_letter import InMemoryDeadLetterQueue
from prodagent.backends.memory.graph import InMemoryGraphStore
from prodagent.backends.memory.idempotency import InMemoryIdempotencyStore
from prodagent.backends.memory.lock import InProcessLockStore
from prodagent.backends.memory.vector import InMemoryVectorStore

__all__ = [
    "InMemoryApprovalStore",
    "InMemoryCache",
    "InMemoryDeadLetterQueue",
    "InMemoryGraphStore",
    "InMemoryIdempotencyStore",
    "InProcessLockStore",
    "InMemoryVectorStore",
]
