"""The agent factory — builds and runs the real agent for one golden example."""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import replace as _dc_replace
from pathlib import Path

from prodagent.core.config import FrameworkConfig
from prodagent.core.state import AgentRun
from prodagent.core.types import RunState
from prodagent.evaluation.evals.dataset import GoldenExample
from prodagent.evaluation.evals.judge import LLMJudge
from prodagent.llm.base import LLMConfig

from aiops.agent import build_aiops_agent
from aiops.testing.fake_llm_scripts import oom_happy_path_script


async def run_example(example: GoldenExample) -> AgentRun:
    """Build the real agent and run one golden example end-to-end.

    The agent under test runs on a FakeLLM script so the eval is deterministic
    — it tests the framework's orchestration, not the LLM's intelligence. The
    judge uses a separate, real LLM client to grade.

    ``USE_FAKE_LLM`` is set so the aux LLM (memory classify + skill synthesis
    at SESSION_END) also uses the offline routing fake — otherwise those
    background calls would hit the real API and break determinism. The main
    ``llm=`` is passed explicitly below, so the env only affects the aux path.

    Each call gets a FRESH file-backed checkpoint + event log under a unique
    temp directory and a unique run_id. Isolated stores make every eval a clean
    run — two runs sharing a run_id on disk would let the second resume the
    first's half-written phases.
    """
    os.environ.setdefault("USE_FAKE_LLM", "1")
    run_id = f"{example.id}-{uuid.uuid4().hex[:8]}"
    run_dir = Path(tempfile.mkdtemp(prefix=f"prodagent-eval-{run_id}-"))
    # Per-run isolated fw: runs_dir/events_dir 指向独立 temp 目录,让框架
    # lazy resolve 出来的 checkpoint/event_log 互相隔离,run 之间不串台。
    fw = FrameworkConfig.default()
    fw = _dc_replace(fw, orchestration=_dc_replace(
        fw.orchestration,
        runs_dir=str(run_dir / "checkpoints"),
        events_dir=str(run_dir / "events"),
    ))
    agent = build_aiops_agent(
        llm=oom_happy_path_script(),
        framework_config=fw,
    )
    # Auto-approve HIGH-risk tools so the scripted trajectory runs end-to-end —
    # the eval tests orchestration, not the approval gate.
    run = await agent.chat(example.task, session_id=run_id)
    while run.state is RunState.SUSPENDED and run.pending_approval_id:
        await agent.submit_approval(run.pending_approval_id, "brief_approval")
        run = await agent.chat(resume=True, session_id=run_id)
    return run


# ── Judge wiring — a model distinct from the agent under test ──────────────────
_JUDGE_MODEL = os.getenv("JUDGE_MODEL", "").strip()

# Two thresholds, two jobs (the "brake vs dashboard" split from lesson 19):
_PASS_THRESHOLD = float(os.getenv("JUDGE_PASS_THRESHOLD", "0.60"))
_BLOCKING_THRESHOLD = float(os.getenv("JUDGE_BLOCKING_THRESHOLD", "0.50"))


def build_judge(pass_threshold: float = _PASS_THRESHOLD) -> LLMJudge:
    """An independent LLM judge. Set JUDGE_MODEL to pin a model ≠ the agent's."""
    from prodagent.backends.factory import resolve_llm

    judge_llm = resolve_llm(None, config=LLMConfig(model=_JUDGE_MODEL) if _JUDGE_MODEL else None)
    return LLMJudge(
        llm=judge_llm,
        pass_threshold=pass_threshold,
        blocking_threshold=_BLOCKING_THRESHOLD,
    )
