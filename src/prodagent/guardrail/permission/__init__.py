"""prodagent.guardrail.permission — agent permission granularity."""

from __future__ import annotations

from prodagent.guardrail.permission.circuit_breaker import PermissionCircuitBreaker
from prodagent.guardrail.permission.scopes import (
    PermissionMatrix,
    PermissionMatrixBuilder,
    PermissionScope,
)
from prodagent.guardrail.permission.taint import (
    ContextTaintMonitor,
    TaintLevel,
)

__all__ = [
    "PermissionMatrix",
    "PermissionMatrixBuilder",
    "PermissionScope",
    "PermissionCircuitBreaker",
    "ContextTaintMonitor",
    "TaintLevel",
]
