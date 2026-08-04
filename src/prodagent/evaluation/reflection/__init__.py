"""reflection — ch20 four-layer cognitive chain, layer 1.

Layer 1 — real-time mirror: ``ConstitutionalChecker`` applies principles to an
agent's output, flagging violations and (when an LLM is available) revising
them. Layers 2 (experience ledger) and 3 (skill emergence) live in
``prodagent.evaluation.learning`` and ``prodagent.evaluation.skills``. Layer 4
(DPO gene-reshaping + two-tier rollback) is future work.
"""

from __future__ import annotations

from prodagent.evaluation.reflection.constitutional import (
    ConstitutionalChecker,
    ConstitutionalPrinciple,
    ConstitutionalResult,
)

__all__ = [
    "ConstitutionalChecker",
    "ConstitutionalPrinciple",
    "ConstitutionalResult",
]
