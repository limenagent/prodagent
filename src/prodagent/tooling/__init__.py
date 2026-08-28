"""tooling — the tool surface: definition, registry, dispatch, and care.

``base`` wraps a Python function as a callable tool (schema + coercion +
the single throat results pass through); ``decorator`` is the authoring
syntax sugar; ``registry`` names them; ``dispatcher`` executes batches with
reliability policy (circuit breaker, timeouts); ``merge`` and ``search``
shape tool menus for the model; ``skill_resolver`` routes skill loads.
"""

from prodagent.tooling.base import FunctionTool
from prodagent.tooling.decorator import tool
from prodagent.tooling.dispatcher import ToolDispatcher
from prodagent.tooling.registry import ToolRegistry
from prodagent.tooling.skill_resolver import SkillResolver

__all__ = [
    "FunctionTool",
    "tool",
    "ToolRegistry",
    "ToolDispatcher",
    "SkillResolver",
]
