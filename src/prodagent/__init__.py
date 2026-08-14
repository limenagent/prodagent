"""prodagent — production-grade LLM agent framework."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version
from typing import Any

try:
    __version__ = _version("prodagent")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"

__all__ = [
    "__version__",
    "Agent",
    "AgentConfig",
    "tool",
    "HardBudget",
    "ExecutionMode",
    "RunState",
    "RunPhase",
    "ToolMeta",
    "SideEffectLevel",
    "ToolError",
    "ToolResult",
    "ErrorReason",
    "ErrorLayer",
    "ClassifiedError",
    "classify_error",
    "BudgetExceeded",
    "PromptInjectionDetected",
    "SecurityViolation",
    "VersionConflict",
    "CorruptedCheckpointError",
]

_SYMBOL_SOURCES: dict[str, str] = {
    "Agent": "prodagent.runtime.agent",
    "AgentConfig": "prodagent.runtime.config",
    "HardBudget": "prodagent.core.budget",
    "BudgetExceeded": "prodagent.core.exceptions",
    "CorruptedCheckpointError": "prodagent.core.exceptions",
    "PromptInjectionDetected": "prodagent.core.exceptions",
    "SecurityViolation": "prodagent.core.exceptions",
    "VersionConflict": "prodagent.core.exceptions",
    "ExecutionMode": "prodagent.core.types",
    "RunState": "prodagent.core.types",
    "RunPhase": "prodagent.core.types",
    "SideEffectLevel": "prodagent.core.types",
    "ToolError": "prodagent.core.types",
    "ToolMeta": "prodagent.core.types",
    "ToolResult": "prodagent.core.types",
    "ErrorReason": "prodagent.core.error_reason",
    "ErrorLayer": "prodagent.core.error_reason",
    "ClassifiedError": "prodagent.core.error_classifier",
    "classify_error": "prodagent.core.error_classifier",
    "tool": "prodagent.tooling.decorator",
}


def __getattr__(name: str) -> Any:
    source = _SYMBOL_SOURCES.get(name)
    if source is None:
        raise AttributeError(f"module 'prodagent' has no attribute {name!r}")
    import importlib

    module = importlib.import_module(source)
    try:
        value = getattr(module, name)
    except AttributeError:
        raise AttributeError(f"module {source!r} has no attribute {name!r}") from None
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | {"__version__"})
