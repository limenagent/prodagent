"""Five-level compression — escalate only as the window fills.

NONE → tool-result compress → history summary → topic summary → emergency:
each stage runs only when the budget tracker says the previous one wasn't
enough, so cheap mechanical shrinking happens long before any LLM
summarisation is spent. Re-exported surface for the pipeline stages."""

from __future__ import annotations

from prodagent.cognition.context.compression.formatting import CHARS_PER_TOKEN
from prodagent.cognition.context.compression.pipeline import (
    EmergencyStage,
    HistoryCompressor,
    NoCompressionStage,
    Stage,
    StageContext,
    SummarizeStage,
    ToolCompressStage,
    fit_budget,
    safe_tail_start,
)
from prodagent.cognition.context.compression.summarizer import Summariser

__all__ = [
    "Stage",
    "StageContext",
    "HistoryCompressor",
    "NoCompressionStage",
    "ToolCompressStage",
    "SummarizeStage",
    "EmergencyStage",
    "Summariser",
    "fit_budget",
    "safe_tail_start",
    "CHARS_PER_TOKEN",
]
