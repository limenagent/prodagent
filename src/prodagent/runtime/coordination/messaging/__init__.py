"""The communication substrate under all five collaboration primitives.

``agents=`` (tree), ``peers=`` (chain), Ensemble (floor), Blackboard (board),
WorkQueue (queue) differ in topology, but collaboration in every one of them
is messages crossing agent boundaries. This package is the single checkpoint
those crossings flow through:

- :mod:`envelope` — :class:`Crossing`, one boundary traversal in a uniform
  envelope (identity, lineage, direction, kind, typed payload).
- :mod:`contract` — :class:`MessageContract`, structural admission.
- :mod:`pipeline` — :class:`Pipeline` (fixed slot order, dead-letter error
  boundary) plus the two presets: :func:`admission_pipeline` (UPSTREAM) and
  :func:`assembly_pipeline` (DOWNSTREAM).
- :mod:`interceptors` — built-in capabilities: dedupe, contract, trim,
  projection, security gate, audit. Mechanics only; semantic policy is
  user-injected at the open slots.

Direction is the primary axis: DOWNSTREAM crossings are assembled by
deterministic code (the container is the whitelist); UPSTREAM crossings carry
LLM-generated content and are admitted by a gatekeeper. Poisoning defense —
the reason this plane exists — is just the admission + projection capability
families applied consistently; identity, observability, and budget accounting
ride the same crossings for free.
"""

from prodagent.runtime.coordination.messaging.contract import (
    DEFAULT_CHILD_CONTRACT as DEFAULT_CHILD_CONTRACT,
)
from prodagent.runtime.coordination.messaging.contract import (
    MessageContract as MessageContract,
)
from prodagent.runtime.coordination.messaging.envelope import (
    Crossing as Crossing,
)
from prodagent.runtime.coordination.messaging.envelope import (
    CrossingKind as CrossingKind,
)
from prodagent.runtime.coordination.messaging.envelope import (
    CrossingRejected as CrossingRejected,
)
from prodagent.runtime.coordination.messaging.envelope import (
    CrossingStopped as CrossingStopped,
)
from prodagent.runtime.coordination.messaging.envelope import (
    Delivery as Delivery,
)
from prodagent.runtime.coordination.messaging.envelope import (
    Direction as Direction,
)
from prodagent.runtime.coordination.messaging.envelope import (
    DuplicateCrossing as DuplicateCrossing,
)
from prodagent.runtime.coordination.messaging.idempotency import (
    IdempotentMessageHandler as IdempotentMessageHandler,
)
from prodagent.runtime.coordination.messaging.interceptors import (
    AuditInterceptor as AuditInterceptor,
)
from prodagent.runtime.coordination.messaging.interceptors import (
    ContractInterceptor as ContractInterceptor,
)
from prodagent.runtime.coordination.messaging.interceptors import (
    DedupeInterceptor as DedupeInterceptor,
)
from prodagent.runtime.coordination.messaging.interceptors import (
    GateInterceptor as GateInterceptor,
)
from prodagent.runtime.coordination.messaging.interceptors import (
    ProjectionInterceptor as ProjectionInterceptor,
)
from prodagent.runtime.coordination.messaging.interceptors import (
    TrimInterceptor as TrimInterceptor,
)
from prodagent.runtime.coordination.messaging.interceptors import (
    handoff_data_for as handoff_data_for,
)
from prodagent.runtime.coordination.messaging.packet import (
    HandoffPacket as HandoffPacket,
)
from prodagent.runtime.coordination.messaging.pipeline import (
    Interceptor as Interceptor,
)
from prodagent.runtime.coordination.messaging.pipeline import (
    Pipeline as Pipeline,
)
from prodagent.runtime.coordination.messaging.pipeline import (
    Slot as Slot,
)
from prodagent.runtime.coordination.messaging.pipeline import (
    admission_pipeline as admission_pipeline,
)
from prodagent.runtime.coordination.messaging.pipeline import (
    assembly_pipeline as assembly_pipeline,
)
