from __future__ import annotations

import json

from prodagent.kernel.types import LLMResponse
from prodagent.llm.fake import FakeLLMAdapter


def _plan_llm() -> FakeLLMAdapter:
    plan = {
        "steps": [
            {"id": "s1", "action": "collect", "params": {}, "depends_on": []},
            {"id": "s2", "action": "report", "params": {}, "depends_on": ["s1"], "terminal": True},
        ]
    }
    return FakeLLMAdapter(responses=[LLMResponse(content=json.dumps(plan), stop_reason="end_turn")])
