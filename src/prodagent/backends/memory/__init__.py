"""In-process, ephemeral backend implementations.

Default for ephemeral state (approval, locks, dead-letter, LLM response
cache). State dies with the process — fine for single-host runs and tests.
For multi-replica deployments, swap in ``prodagent.backends.redis``.
"""

from prodagent.backends.memory.approval import InMemoryApprovalStore
from prodagent.backends.memory.blob import InMemoryBlobStore
from prodagent.backends.memory.cache import InMemoryCache
from prodagent.backends.memory.event_log import InMemoryEventLog
from prodagent.backends.memory.graph import InMemoryGraphStore
from prodagent.backends.memory.lock import InProcessLockStore

__all__ = [
    "InMemoryApprovalStore",
    "InMemoryBlobStore",
    "InMemoryCache",
    "InMemoryEventLog",
    "InMemoryGraphStore",
    "InProcessLockStore",
]
