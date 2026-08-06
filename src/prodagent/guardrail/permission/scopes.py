"""Three-dimensional permission matrix — deny-by-default."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from prodagent.core.exceptions import PermissionDenied


@dataclass
class PermissionScope:
    operations: set[str]
    objects: set[str]
    param_constraints: dict[str, Any] = field(default_factory=dict)


class PermissionMatrixBuilder:
    def __init__(self, agent_id: str) -> None:
        self._agent_id = agent_id
        self._scopes: list[PermissionScope] = []

    def allow(
        self,
        *,
        operations: set[str],
        objects: set[str],
        constraints: dict[str, Any] | None = None,
    ) -> PermissionMatrixBuilder:
        if "*" in objects:
            raise ValueError(
                "PermissionMatrix.allow(): objects={'*'} is not allowed. "
                "Enumerate the exact object names the agent may access."
            )
        self._scopes.append(
            PermissionScope(
                operations=set(operations),
                objects=set(objects),
                param_constraints=constraints or {},
            )
        )
        return self

    def build(self) -> PermissionMatrix:
        return PermissionMatrix(
            agent_id=self._agent_id,
            scopes=list(self._scopes),
        )


@dataclass
class PermissionMatrix:
    agent_id: str
    scopes: list[PermissionScope] = field(default_factory=list)

    @staticmethod
    def builder(agent_id: str) -> PermissionMatrixBuilder:
        return PermissionMatrixBuilder(agent_id)

    def allows(
        self,
        operation: str,
        obj: str,
        params: dict[str, Any] | None = None,
    ) -> bool:
        for scope in self.scopes:
            if operation not in scope.operations:
                continue
            if obj not in scope.objects:
                continue
            if scope.param_constraints:
                params = params or {}
                violated = False
                for key, limit in scope.param_constraints.items():
                    val = params.get(key)
                    if val is None:
                        violated = True
                        break
                    if isinstance(limit, int | float) and isinstance(val, int | float):
                        if val > limit:
                            violated = True
                            break
                    elif isinstance(limit, list) and val not in limit:
                        violated = True
                        break
                if violated:
                    continue
            return True
        return False  # deny by default

    def assert_allows(
        self,
        operation: str,
        obj: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        if not self.allows(operation, obj, params):
            raise PermissionDenied(
                f"Agent '{self.agent_id}' lacks permission: {operation} on '{obj}'",
                agent_id=self.agent_id,
                operation=operation,
                object=obj,
            )
