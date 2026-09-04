"""channels —— 状态通道与合并规则（对应第 07 课，内核最精华的设计之一）。

为什么状态不是一个普通 dict、写的时候直接 `state[k] = v`？
因为波次内多个节点是并发跑的，它们可能同时写同一个键：
- 消息历史这种，应该“追加”而不是互相覆盖；
- 成本这种，应该“相加”；
- 当前阶段这种，才是“后写覆盖”。

于是我们让每个键（通道）显式声明一个 reducer：(旧值, 新值) -> 合并值。
调度器在波次屏障处统一按 reducer 折叠，并发结果就是确定的，与谁先跑完无关。
reducer 必须是纯函数——这也是状态能靠事件重放出来的前提（第 12 课）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Reducer = Callable[[Any, Any], Any]


def _last(old: Any, new: Any) -> Any:
    return new


def _append(old: Any, new: Any) -> list:
    base = list(old or [])
    if isinstance(new, list):
        base.extend(new)
    else:
        base.append(new)
    return base


def _add(old: Any, new: Any) -> Any:
    return (old or 0) + (new or 0)


def _merge(old: Any, new: Any) -> dict:
    out = dict(old or {})
    out.update(new or {})
    return out


@dataclass(frozen=True)
class Channel:
    """一个状态通道：初始值 + 合并规则 + 中性元。

    allow_multi 标记同一波内是否允许多个节点并发写它：追加/求和/合并满足结合
    交换，允许；last 通道并发写结果取决于调度顺序，属于歧义写入，默认禁止。

    empty 是这个合并规则的“中性元”，用来把同一波里对同一通道的多次写入先
    聚合成一个“波增量”：事件流记录波增量，重放时才不会重复累计。
    """

    init: Any
    reducer: Reducer
    allow_multi: bool = True
    empty: Any = None

    def fold(self, old: Any, new: Any) -> Any:
        return self.reducer(old, new)


# —— 四个常用通道的工厂，名字即语义 ——
def last(init: Any = None) -> Channel:
    """后写覆盖：当前阶段、最终结论这类单值。"""
    return Channel(init, _last, allow_multi=False, empty=None)


def append(init: Any = None) -> Channel:
    """追加成列表：消息历史、检索片段。"""
    return Channel(list(init or []), _append, allow_multi=True, empty=[])


def add(init: Any = 0) -> Channel:
    """数值累加：成本、计数。"""
    return Channel(init, _add, allow_multi=True, empty=0)


def merge(init: Any = None) -> Channel:
    """字典合并：结构化结果拼合。"""
    return Channel(dict(init or {}), _merge, allow_multi=True, empty={})


class AmbiguousWrite(RuntimeError):  # noqa: N818 —— 名字直指语义，教学优先于命名惯例
    """同一波内，多个节点并发写了一个 last 通道——结果不确定，必须显式处理。"""


@dataclass
class _Write:
    key: str
    value: Any
    writer: str  # 是哪个节点写的，报错时能定位


class WaveWrites:
    """收集“这一波”所有节点的状态增量，屏障后一次性折叠（对应第 16 课）。

    节点在执行过程中不直接改共享状态，只把增量交到这里，从根上避免了
    “读到另一个节点跑到一半的半成品状态”这种并发竞态。
    """

    def __init__(self, channels: dict[str, Channel]):
        self._channels = channels
        self._buffer: list[_Write] = []

    def buffer(self, key: str, value: Any, writer: str) -> None:
        channel = self._channels.get(key)
        if channel is None:
            # 写入一个从未声明的通道，几乎一定是笔误或设计遗漏，早报错比静默丢强。
            raise KeyError(f"写入了未声明的状态通道 {key!r}（来自节点 {writer!r}）")
        self._buffer.append(_Write(key, value, writer))

    def check_ambiguous(self) -> None:
        """屏障处检查：last 通道是否被同一波的多个节点写了。"""
        writers_by_key: dict[str, set[str]] = {}
        for w in self._buffer:
            writers_by_key.setdefault(w.key, set()).add(w.writer)
        for key, writers in writers_by_key.items():
            channel = self._channels[key]
            if not channel.allow_multi and len(writers) > 1:
                raise AmbiguousWrite(
                    f"通道 {key!r} 是 last（后写覆盖），却被同波多个节点 {writers} 并发写，"
                    f"结果取决于调度顺序；请改用 append/add/merge，或只让一个节点写它。"
                )

    def drain(self) -> list[_Write]:
        """按进入顺序取出本波全部增量，并清空缓冲。"""
        out = self._buffer
        self._buffer = []
        return out
