"""evaluation — testing, evals, learning, skills, reflection."""

from prodagent.evaluation.evals.dataset import (
    EvalReport,
    ExampleResult,
    GoldenDataset,
    GoldenExample,
)
from prodagent.evaluation.evals.judge import (
    DimensionScore,
    JudgeVerdict,
    LLMJudge,
    compare_trajectories,
)
from prodagent.evaluation.evals.runner import (
    EvalRunner,
    RegressionDetector,
    RegressionLevel,
)
from prodagent.evaluation.learning.experience import (
    ExperienceOutcome,
    ExperienceRecord,
)
from prodagent.evaluation.learning.skill_synthesizer import SkillSynthesizer
from prodagent.evaluation.learning.storage import FileExperienceStore
from prodagent.evaluation.reflection.constitutional import (
    ConstitutionalChecker,
    ConstitutionalPrinciple,
    ConstitutionalResult,
)
from prodagent.evaluation.skills.registry import (
    SkillCard,
    SkillContent,
    SkillRegistry,
)
from prodagent.evaluation.testing.cassette import RecordingLLMClient, ReplayLLMClient
from prodagent.evaluation.testing.trace_assert import TrajectoryAssert
from prodagent.llm.fake import FakeLLMAdapter, script

__all__ = [
    # Dataset
    "GoldenDataset",
    "GoldenExample",
    "EvalReport",
    "ExampleResult",
    # Runner + regression
    "EvalRunner",
    "RegressionDetector",
    "RegressionLevel",
    # LLM-as-Judge
    "LLMJudge",
    "JudgeVerdict",
    "DimensionScore",
    "compare_trajectories",
    # Testing
    "FakeLLMAdapter",
    "script",
    "RecordingLLMClient",
    "ReplayLLMClient",
    "TrajectoryAssert",
    # Experience / learning
    "ExperienceOutcome",
    "ExperienceRecord",
    "FileExperienceStore",
    "SkillSynthesizer",
    # Reflection (ch20 layer 1)
    "ConstitutionalChecker",
    "ConstitutionalPrinciple",
    "ConstitutionalResult",
    # Skills
    "SkillCard",
    "SkillContent",
    "SkillRegistry",
]
