"""Make sure the workspace's src package is importable no matter where pytest is launched from."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def start_react_run(plan, task, history=None, parent=None, *, llm=None, tools=None):
    """Test helper: a ReAct Run with this turn's opening user message, ready
    for a Scheduler to drive (the src-side helper retired when the facade
    slimmed; only tests ever needed the composition)."""
    from src.kernel import Run
    from src.runtime.react import opening

    initial = opening(task, history) if task else None
    if parent is not None:
        return Run.child_of(parent, plan, task=task, input=initial, llm=llm, tools=tools)
    return Run.start(plan, task=task, input=initial, llm=llm, tools=tools)
