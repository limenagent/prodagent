"""graph —— 静态蓝图：节点、边、Plan，以及“这一波谁就绪”的计算。

蓝图回答“有哪些步骤、它们怎么连、状态长什么样”，它不包含任何一次运行的
动态数据（跑到哪了、当前状态是什么）——那些属于 Run 的核心分离。
同一张 Plan 可以同时被很多个 Run 复用。

这里唯一有点“动态”的是 ready()：给定一次 Run 的当前状态，算出下一波
可以跑的节点。它是纯读判断，不修改任何东西，这让调度逻辑非常好测。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.kernel.channels import Channel

# 条件边谓词：只看当前共享状态，决定这条边是否“通”。
EdgePredicate = Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class Edge:
    """一条有向边。when 为 None 表示恒通；否则按当前状态判断是否激活。"""

    source: str
    target: str
    when: EdgePredicate | None = None


@dataclass(frozen=True)
class RetryPolicy:
    """步骤级弹性策略（机制只负责执行，是否重试、退避多久都是可替换配置）。

    - max_attempts：含首次在内的最大尝试次数；
    - base_delay/factor：指数退避，第 n 次失败后等 base_delay * factor**n；
    - retry_on：只有这些异常才重试，外部取消（CancelledError）永不重试。
    """

    max_attempts: int = 3
    base_delay: float = 0.05
    factor: float = 2.0
    retry_on: tuple[type, ...] = (Exception,)

    def delay_for(self, failed_attempt: int) -> float:
        # failed_attempt 从 0 起：第一次失败后等 base_delay，第二次等 base_delay*factor。
        return self.base_delay * (self.factor**failed_attempt)


@dataclass(frozen=True)
class Node:
    """图里的一个步骤。

    - body：真正干活的可组合体（见 body.py），蓝图不关心它内部是什么；
    - join：多个前驱时，all=全部完成才就绪，any=任一完成即就绪；
    - template：它是“动态扇出模板”，运行时由 Send 实例化多次；
    - terminal：标记它是产出最终结果的汇聚节点；
    - timeout：单次执行的最长秒数，超时算一次失败；
    - retry：失败后的重试/退避策略，不给就只执行一次、失败即失败。
    """

    id: str
    body: Any
    join: str = "all"
    template: bool = False
    terminal: bool = False
    timeout: float | None = None
    retry: RetryPolicy | None = None


@dataclass
class Plan:
    """一张静态执行蓝图 = 节点表 + 边 + 状态通道声明 + 入口。"""

    channels: dict[str, Channel] = field(default_factory=dict)
    entry: tuple[str, ...] = ()
    _nodes: dict[str, Node] = field(default_factory=dict, init=False)
    _incoming: dict[str, list[Edge]] = field(default_factory=dict, init=False)
    _outgoing: dict[str, list[Edge]] = field(default_factory=dict, init=False)

    # —— 构建期：链式声明，构建完会做一次校验 ——
    def add(self, *nodes: Node) -> Plan:
        for n in nodes:
            if n.id in self._nodes:
                raise ValueError(f"节点 id 重复：{n.id}")
            self._nodes[n.id] = n
            self._incoming.setdefault(n.id, [])
            self._outgoing.setdefault(n.id, [])
        return self

    def edge(self, source: str, target: str, when: EdgePredicate | None = None) -> Plan:
        e = Edge(source, target, when)
        self._outgoing.setdefault(source, []).append(e)
        self._incoming.setdefault(target, []).append(e)
        return self

    def validate(self) -> Plan:
        """编译期体检：悬空边、未知入口在这里就报错，而不是跑到一半才炸。"""
        known = set(self._nodes)
        for src, outs in self._outgoing.items():
            if src not in known:
                raise ValueError(f"边的源节点 {src!r} 不存在")
            for e in outs:
                if e.target not in known:
                    raise ValueError(f"边 {src}->{e.target} 的目标不存在")
        for en in self.entry:
            if en not in known:
                raise ValueError(f"入口节点 {en!r} 不存在")
        if not self.entry:
            # 没显式指定入口，就把“没有入边”的节点当入口。
            self.entry = tuple(nid for nid in known if not self._incoming[nid])
        return self

    # —— 读访问 ——
    @property
    def nodes(self) -> dict[str, Node]:
        return self._nodes

    def get(self, node_id: str) -> Node:
        return self._nodes[node_id]

    def incoming(self, node_id: str) -> list[Edge]:
        return list(self._incoming.get(node_id, ()))

    def initial_shared(self) -> dict[str, Any]:
        """每个通道用声明的初始值起步。"""
        return {k: ch.init for k, ch in self.channels.items()}

    def terminal_ids(self) -> list[str]:
        marked = [nid for nid, n in self._nodes.items() if n.terminal]
        if marked:
            return marked
        # 没标 terminal，就把没有出边的节点视作终点。
        return [nid for nid in self._nodes if not self._outgoing[nid]]

    # —— 前驱判定 ——
    @staticmethod
    def _edge_live(e: Edge, shared: dict[str, Any]) -> bool:
        return e.when is None or bool(e.when(shared))

    def _predecessor_done(self, run: Any, source: str) -> bool:
        """前驱“成功完成”：普通节点看 COMPLETED；模板看其所有实例都到终态。"""
        node = self._nodes[source]
        if not node.template:
            return run.is_completed(source)
        instances = run.instances.get(source, ())
        return bool(instances) and all(run.is_terminal(k) for k in instances)

    def _predecessor_terminal(self, run: Any, source: str) -> bool:
        """前驱是否已到任一终态（完成/跳过/失败）。"""
        node = self._nodes[source]
        if not node.template:
            return run.is_terminal(source)
        instances = run.instances.get(source, ())
        return bool(instances) and all(run.is_terminal(k) for k in instances)

    # —— 核心：这一波谁就绪 ——
    def ready(self, run: Any) -> list[str]:
        """纯函数式地算出当前可立即执行的节点/实例 id 列表。

        条件边“此刻不通”时，只要前驱还没到终态，就只是“再等等”，绝不提前
        判死——前驱下一波可能改状态让边变通（ReAct 回边正是如此）。真正走
        不通的死分支，交给 sweep_skipped 在整张图停摆时统一清理。
        """
        ready: list[str] = []
        # 模板节点本身不参与调度，它只通过 Send 出的实例执行。
        static_keys = [nid for nid, n in self._nodes.items() if not n.template]
        all_keys = static_keys + [k for ks in run.instances.values() for k in ks]
        for key in all_keys:
            if not run.is_pending(key):
                continue
            if run.is_instance(key):  # 动态实例：Send 直接激活，pending 即可跑
                ready.append(key)
                continue
            preds = self._incoming.get(key, ())
            # 起点：显式入口，或被 Goto 命令显式激活的节点，pending 即可起步。
            if key in self.entry or key in run.activated:
                ready.append(key)
                continue
            if not preds:
                # 显式 entry 下，没有入边又不是入口/未被命令激活的节点不自动起步，
                # 只等 Goto/Send 到达，避免孤立节点第一波被误当源点。
                continue

            live, has_open_pred = False, False
            for e in preds:
                if not self._predecessor_terminal(run, e.source):
                    has_open_pred = True  # 前驱还没跑完，再等一波
                    continue
                if self._predecessor_done(run, e.source) and self._edge_live(e, run.shared):
                    live = True  # any：任一活边即就绪；all：此刻已无未完成前驱
            if has_open_pred:
                continue
            if live:
                ready.append(key)
            # 前驱都终态却仍无活边：保持 pending，停摆时由 sweep_skipped 清理。
        return ready

    def sweep_skipped(self, run: Any) -> None:
        """死分支清理：前驱全部终态、却没有一条活边的 pending 节点标记跳过。

        级联到不动点。只在“这一波没有任何节点就绪”时调用，因此不会误杀
        将来才可能被激活的分支（比如被回边反复驱动的节点）。
        """
        static_keys = [nid for nid, n in self._nodes.items() if not n.template]
        changed = True
        while changed:
            changed = False
            for key in static_keys:
                if not run.is_pending(key) or key in self.entry:
                    continue
                preds = self._incoming.get(key, ())
                if not preds:
                    continue
                all_terminal = all(self._predecessor_terminal(run, e.source) for e in preds)
                any_live = any(
                    self._predecessor_done(run, e.source) and self._edge_live(e, run.shared)
                    for e in preds
                )
                if all_terminal and not any_live:
                    run.mark_skipped(key)
                    changed = True

    def is_done(self, run: Any) -> bool:
        """所有（非模板）静态节点与动态实例都到了终态（完成/跳过/失败）。"""
        static_keys = [nid for nid, n in self._nodes.items() if not n.template]
        keys = static_keys + [k for ks in run.instances.values() for k in ks]
        return all(run.is_terminal(k) for k in keys)
