"""prodagent — production-grade LLM agent framework.

The bare kernel: loop + tools + LLM port + event stream, zero disk.
``production()`` (prodagent.core.config) restores the full stack.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from prodagent.core.lazy import lazy_package

try:
    __version__ = _version("prodagent")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"

_SYMBOL_SOURCES: dict[str, str] = {
    "Agent": "prodagent.runtime.agent",
    "AgentConfig": "prodagent.runtime.config",
    "HardBudget": "prodagent.kernel.budget",
    "BudgetExceeded": "prodagent.core.exceptions",
    "CorruptedCheckpointError": "prodagent.core.exceptions",
    "PromptInjectionDetected": "prodagent.core.exceptions",
    "SensitiveContentDetected": "prodagent.core.exceptions",
    "SecurityViolation": "prodagent.core.exceptions",
    "VersionConflict": "prodagent.core.exceptions",
    "ExecutionMode": "prodagent.kernel.types",
    "RunState": "prodagent.kernel.types",
    "SideEffectLevel": "prodagent.kernel.types",
    "ToolError": "prodagent.kernel.types",
    "ToolMeta": "prodagent.kernel.types",
    "ToolResult": "prodagent.kernel.types",
    "ErrorReason": "prodagent.core.error_reason",
    "ErrorLayer": "prodagent.core.error_reason",
    "ClassifiedError": "prodagent.core.error_classifier",
    "classify_error": "prodagent.core.error_classifier",
    "tool": "prodagent.tooling.decorator",
    "FrameworkConfig": "prodagent.core.config",
    "ContextConfig": "prodagent.core.config",
    "LLMClient": "prodagent.llm",
    "LLMConfig": "prodagent.llm",
    "FakeLLMAdapter": "prodagent.llm.fake",
    "RoutingFakeLLM": "prodagent.llm.fake",
    "script": "prodagent.llm.fake",
    "use_fake_llm": "prodagent.llm.providers",
    "Ensemble": "prodagent.coordination.ensemble",
    "RoundRobin": "prodagent.coordination.ensemble",
    "Moderated": "prodagent.coordination.ensemble",
    "FreeForAll": "prodagent.coordination.ensemble",
    "WorkQueue": "prodagent.coordination.work_queue",
    "Board": "prodagent.coordination.blackboard",
    "BoardWrite": "prodagent.coordination.blackboard",
    "Trigger": "prodagent.coordination.blackboard",
    "TerminationPolicy": "prodagent.coordination.termination",
    "MaxRounds": "prodagent.coordination.termination",
    "BudgetLedger": "prodagent.kernel.budget",
    "MemoryManager": "prodagent.cognition.memory",
    "build_memory_manager": "prodagent.cognition.memory",
}

__all__ = ["__version__", *sorted(_SYMBOL_SOURCES)]

__getattr__, __dir__ = lazy_package(_SYMBOL_SOURCES)
