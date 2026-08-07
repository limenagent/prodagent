"""小美的记忆 —— 预埋一条关于**大牛**的 CONSTRAINT
"""

from __future__ import annotations

import shutil
from pathlib import Path

from prodagent.backends.memory.graph import InMemoryGraphStore
from prodagent.cognition.memory import MemoryManager, build_memory_manager
from prodagent.cognition.memory.storage import MemoryRecord, MemoryType

_BASE = Path(__file__).parent
MEMORY_DIR = _BASE / ".memory"

NIU_MATCHMAKER_HINT = "介绍人提前说过：大牛这人大大咧咧，做事不太仔细，丢三落四的毛病一直没改。"


def build_memory(*, clean: bool = False, memory_dir: Path | None = None) -> MemoryManager:
    """构造持久化 MemoryManager —— documents 落盘，facts 常驻内存，不挂分类器。"""
    target_dir = memory_dir or MEMORY_DIR
    if clean and target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    from prodagent.backends.file.document import FileDocumentStore

    return build_memory_manager(
        documents=FileDocumentStore(target_dir),
        facts=InMemoryGraphStore(),
    )


async def seed_mei_memory(memory: MemoryManager) -> None:
    """预埋介绍人对大牛的评价——记的是对方的信息，不是小美自己的信息。"""
    await memory.add_memory(
        MemoryRecord(
            content=NIU_MATCHMAKER_HINT,
            memory_type=MemoryType.CONSTRAINT,
            domain="dating",
            source="介绍人",
        )
    )


__all__ = [
    "MEMORY_DIR",
    "NIU_MATCHMAKER_HINT",
    "build_memory",
    "seed_mei_memory",
]
