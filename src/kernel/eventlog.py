"""eventlog —— 事件是唯一事实源，状态是事件流折叠出的投影（第 11、12、13 课）。

三件东西：
- Event：不可变的“已经发生的事实”，只追加、不修改；
- apply_event：纯函数，把一个事件 fold 进共享状态——重放事件流就能重建状态；
- EventLog / CheckpointStore 两个存储端口 + 进程内默认实现，生产可换 Redis/PG。

为什么状态要从事件 fold，而不是直接存一个最新 dict？因为事件流同时给了你
审计（怎么走到这一步的）、时间旅行（回到任意一步）和崩溃恢复（重放即可）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from src.kernel.channels import Channel

# 事件种类（教学只保留最能说明问题的几种；生产可以更细）。
RUN_STARTED = "run_started"
NODE_STARTED = "node_started"
NODE_COMPLETED = "node_completed"
NODE_RETRY = "node_retry"
STATE_DELTA = "state_delta"
INTERRUPTED = "interrupted"
RESUMED = "resumed"
RUN_COMPLETED = "run_completed"
RUN_FAILED = "run_failed"


@dataclass(frozen=True)
class Event:
    seq: int                 # 在同一个 run 内单调递增
    run_id: str
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None    # 子 Run 事件借此挂到父 Run，重建 Run 树


def apply_event(shared: dict[str, Any], event: Event,
                channels: dict[str, Channel]) -> None:
    """把单个事件折叠进 shared（原地）。纯函数：同样的事件流必得同样状态。"""
    if event.kind != STATE_DELTA:
        return
    for key, value in event.data.get("delta", {}).items():
        channel = channels[key]
        shared[key] = channel.fold(shared.get(key), value)


def fold_events(events: list[Event], channels: dict[str, Channel],
                initial: dict[str, Any]) -> dict[str, Any]:
    """从初始状态重放一整段事件流（测试与时间旅行用）。"""
    shared = dict(initial)
    for ev in events:
        apply_event(shared, ev, channels)
    return shared


class EventLog(Protocol):
    async def append(self, event: Event) -> int: ...
    async def events(self, run_id: str) -> list[Event]: ...
    async def after(self, run_id: str, since_seq: int) -> list[Event]: ...


class CheckpointStore(Protocol):
    async def save(self, run_id: str, snapshot: dict, *, expected_version: int | None = None) -> int: ...
    async def load(self, run_id: str) -> dict | None: ...


class InMemoryEventLog:
    """进程内事件日志，默认实现；接口就是生产实现要满足的契约。"""

    def __init__(self) -> None:
        self._streams: dict[str, list[Event]] = {}

    async def append(self, event: Event) -> int:
        stream = self._streams.setdefault(event.run_id, [])
        stream.append(event)
        return event.seq

    async def events(self, run_id: str) -> list[Event]:
        return list(self._streams.get(run_id, ()))

    async def after(self, run_id: str, since_seq: int) -> list[Event]:
        return [e for e in self._streams.get(run_id, ()) if e.seq > since_seq]


class InMemoryStore:
    """进程内检查点存储，默认实现；换成数据库只改这一层。"""

    def __init__(self) -> None:
        self._snapshots: dict[str, dict] = {}
        self._version: dict[str, int] = {}

    async def save(self, run_id: str, snapshot: dict, *,
                   expected_version: int | None = None) -> int:
        # 乐观并发：带了期望版本就必须对得上，防止两个执行互相覆盖。
        if expected_version is not None and self._version.get(run_id, 0) != expected_version:
            raise RuntimeError(f"检查点版本冲突：期望 {expected_version}")
        version = self._version.get(run_id, 0) + 1
        self._snapshots[run_id] = snapshot
        self._version[run_id] = version
        return version

    async def load(self, run_id: str) -> dict | None:
        snap = self._snapshots.get(run_id)
        return None if snap is None else dict(snap)

    def version_of(self, run_id: str) -> int:
        return self._version.get(run_id, 0)
