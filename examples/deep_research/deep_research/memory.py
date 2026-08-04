"""Deep Research 预置记忆 —— 避免 LLM 重复查已知的 benchmark。

recall query = run.task,所以「HumanEval」「GPT-4o」等关键词会命中这些预置记忆,
LLM 在 CONTEXT_INJECTOR 阶段看到它们,不重复搜索。
"""

from __future__ import annotations

import shutil
from dataclasses import replace as _dc_replace
from pathlib import Path

from prodagent.cognition.memory import (
    MemoryManager,
    MemoryRecord,
    MemoryType,
)
from prodagent.core.config import FrameworkConfig

_BASE = Path(__file__).parent
MEMORY_DIR = _BASE / ".memory"


def build_memory(
    *,
    framework_config: FrameworkConfig | None = None,
    clean: bool = False,
) -> MemoryManager:
    """构造 MemoryManager + 预置 constraint/entity fact。

    documents/graph store 由 MemoryManager 从 fw lazy resolve —— 这里把 fw
    的 ``runs_dir`` 指向 demo 专属的 ``MEMORY_DIR``,让 store 落在独立目录
    (可被 clean 清空),example 不实例化任何 port 实现。

    classifier/conflict_policy 的 aux LLM 也由 MemoryManager 从 fw lazy
    resolve —— 调用方不需要传。

    Args:
        framework_config: 父 fw;不传时用默认。runs_dir 被覆盖为 MEMORY_DIR。
        clean: demo 用 —— 清掉旧记忆让 demo 确定性。
    """
    if clean and MEMORY_DIR.exists():
        shutil.rmtree(MEMORY_DIR)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    fw = framework_config or FrameworkConfig.default()
    fw = _dc_replace(fw, orchestration=_dc_replace(fw.orchestration, runs_dir=str(MEMORY_DIR)))

    return MemoryManager(framework_config=fw)


async def seed_memory(mgr: MemoryManager) -> None:
    """预置研究相关的 constraint + entity fact,避免 LLM 重复搜索。

    CONSTRAINT 永久强注入 —— recall 必命中。
    FACT 按 entity_id upsert —— 同一 entity 的新版本会 supersede 旧的。
    """
    await mgr.add_memory(MemoryRecord(
        content="HumanEval benchmark 已查过,无需重复搜索 —— GPT-4o 和 Claude 3.5 均在 90%+",
        memory_type=MemoryType.CONSTRAINT,
        entity_id="constraint:humaneval",
        domain="research",
    ))
    await mgr.add_memory(MemoryRecord(
        content="GPT-4o 发布于 2024-05,支持 128K context,Anthropic 工具调用用 antml:tool_use",
        memory_type=MemoryType.FACT,
        entity_id="entity:gpt-4o",
        domain="research",
    ))
    await mgr.add_memory(MemoryRecord(
        content="Claude 3.5 Sonnet 发布于 2024-06,200K context,SWE-bench 领先",
        memory_type=MemoryType.FACT,
        entity_id="entity:claude-3.5",
        domain="research",
    ))
