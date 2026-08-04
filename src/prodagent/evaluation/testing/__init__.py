"""Testing utilities — deterministic test fences: FakeLLM, VCR cassettes."""

from prodagent.evaluation.testing.cassette import RecordingLLMClient, ReplayLLMClient
from prodagent.evaluation.testing.trace_assert import TrajectoryAssert
from prodagent.llm.fake import FakeLLMAdapter, script

__all__ = [
    "FakeLLMAdapter",
    "script",
    "RecordingLLMClient",
    "ReplayLLMClient",
    "TrajectoryAssert",
]
