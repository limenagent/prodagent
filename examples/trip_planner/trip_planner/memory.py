"""Trip Planner 预置记忆 —— 用户偏好,recall 注入让 restaurant peer 知道。

recall query = run.task,所以「拉面」「7 天」「预算 15000」等关键词会
命中这些预置记忆,peer agent 在 CONTEXT_INJECTOR 阶段看到它们。
"""

from __future__ import annotations

import shutil
from dataclasses import replace as _dc_replace
from pathlib import Path

from prodagent.cognition.memory import (
    MemoryClassifier,
    MemoryManager,
    MemoryRecord,
    MemoryType,
)
from prodagent.cognition.memory.conflict import DefaultConflictPolicy
from prodagent.core.config import FrameworkConfig
from prodagent.llm.base import LLMClient

_BASE = Path(__file__).parent
MEMORY_DIR = _BASE / ".memory"


def build_memory(
    *,
    aux_llm: LLMClient,
    framework_config: FrameworkConfig | None = None,
    clean: bool = False,
) -> MemoryManager:
    """构造 MemoryManager + 预置用户偏好。"""
    if clean and MEMORY_DIR.exists():
        shutil.rmtree(MEMORY_DIR)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    fw = framework_config or FrameworkConfig.default()
    fw = _dc_replace(fw, orchestration=_dc_replace(fw.orchestration, runs_dir=str(MEMORY_DIR)))

    return MemoryManager(
        framework_config=fw,
        classifier=MemoryClassifier(aux_llm),
        conflict_policy=DefaultConflictPolicy(llm_client=aux_llm),
    )


async def seed_memory(mgr: MemoryManager) -> None:
    """预置用户偏好 —— PREFERENCE 永久强注入,recall 必命中。"""
    await mgr.add_memory(MemoryRecord(
        content="用户 Alice 偏好拉面和漫画,住酒店要靠近车站,预算 15000 元。",
        memory_type=MemoryType.PREFERENCE,
        entity_id="user:alice",
        domain="travel",
    ))
    await mgr.add_memory(MemoryRecord(
        content="Alice 不吃辣,优先选日式料理;交通偏好新干线。",
        memory_type=MemoryType.PREFERENCE,
        entity_id="user:alice",
        domain="travel",
    ))
