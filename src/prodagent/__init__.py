"""prodagent — production-grade LLM agent framework.

The bare kernel: loop + tools + LLM port + event stream, zero disk.
``production()`` (prodagent.base.config) restores the full stack.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from prodagent.base.lazy import lazy_package

try:
    __version__ = _version("prodagent")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"

_SYMBOL_SOURCES: dict[str, str] = {
    "Agent": "prodagent.runtime.agent",
    "AgentConfig": "prodagent.runtime.config",
    "HardBudget": "prodagent.kernel.budget",
    "BudgetExceeded": "prodagent.base.errors",
    "CorruptedCheckpointError": "prodagent.base.errors",
    "PromptInjectionDetected": "prodagent.base.errors",
    "SensitiveContentDetected": "prodagent.base.errors",
    "SecurityViolation": "prodagent.base.errors",
    "VersionConflict": "prodagent.base.errors",
    "ExecutionMode": "prodagent.kernel.types",
    "RunState": "prodagent.kernel.types",
    "SideEffectLevel": "prodagent.kernel.types",
    "ToolError": "prodagent.kernel.types",
    "ToolMeta": "prodagent.kernel.types",
    "ToolResult": "prodagent.kernel.types",
    "ErrorReason": "prodagent.base.errors",
    "ErrorLayer": "prodagent.base.errors",
    "ClassifiedError": "prodagent.base.errors",
    "classify_error": "prodagent.base.errors",
    "tool": "prodagent.tooling.decorator",
    "FrameworkConfig": "prodagent.base.config",
    "ContextConfig": "prodagent.base.config",
    "LLMClient": "prodagent.llm",
    "LLMConfig": "prodagent.llm",
    "FakeLLMAdapter": "prodagent.llm.fake",
    "RoutingFakeLLM": "prodagent.llm.fake",
    "script": "prodagent.llm.fake",
    "use_fake_llm": "prodagent.llm.providers",
    "Board": "prodagent.coordination.blackboard",
    "BoardWrite": "prodagent.coordination.blackboard",
    "Trigger": "prodagent.coordination.blackboard",
    "TerminationPolicy": "prodagent.coordination.infra.stage",
    "MaxRounds": "prodagent.coordination.infra.stage",
    "BudgetLedger": "prodagent.kernel.budget",
    "MemoryManager": "prodagent.cognition.memory",
    "build_memory_manager": "prodagent.cognition.memory",
}

__all__ = ["__version__", *sorted(_SYMBOL_SOURCES)]

__getattr__, __dir__ = lazy_package(_SYMBOL_SOURCES)
