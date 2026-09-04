"""run —— 一次动态执行：节点运行态、Interrupt 挂起凭证、Run 状态机与快照。

蓝图 Plan 是“图纸”，Run 是“这一次执行”：它持有当前共享状态、每个节点
跑到哪了、父子关系，以及 RUNNING/SUSPENDED/COMPLETED/FAILED 四个状态。
状态转移只有一个入口 _transition，非法跳转直接报错。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from src.kernel.channels import Channel
from src.kernel.types import _ALLOWED_TRANSITIONS, NodeStatus, RunState


def _new_id(prefix: str = "run") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@dataclass
class NodeRuntimeState:
    """一个节点（或动态实例）在本次 Run 里的运行态。"""

    status: NodeStatus = NodeStatus.PENDING
    output: Any = None
    attempts: int = 0

    def mark_running(self) -> None:
        self.status = NodeStatus.RUNNING
        self.attempts += 1

    def mark_completed(self, output: Any) -> None:
        self.status = NodeStatus.COMPLETED
        self.output = output

    def mark_skipped(self) -> None:
        self.status = NodeStatus.SKIPPED

    def mark_failed(self, error: str) -> None:
        self.status = NodeStatus.FAILED
        self.output = error

    def reset_pending(self) -> None:
        """Goto 回边时把节点打回 pending，准备下一波重新执行。"""
        self.status = NodeStatus.PENDING


@dataclass(frozen=True)
class Interrupt:
    """一张“挂起凭证”：在哪个节点、因为什么、要问外界什么。"""

    kind: str  # approval / input / external
    payload: Any = None
    question: str = ""
    node_id: str = ""  # 由 Run 在 park 时补上


class Run:
    """一次 Plan 的执行实例。同一个 Plan 可以同时有很多个互不干扰的 Run。"""

    def __init__(
        self,
        plan: Any,
        run_id: str | None = None,
        *,
        parent_id: str | None = None,
        depth: int = 0,
        task: str = "",
    ):
        self.plan = plan
        self.run_id = run_id or _new_id()
        self.parent_id = parent_id
        self.depth = depth
        self.task = task

        self.shared: dict[str, Any] = plan.initial_shared()
        self.node_states: dict[str, NodeRuntimeState] = {
            nid: NodeRuntimeState() for nid in plan.nodes
        }
        # 动态扇出：模板 id -> 它被实例化出的 key 列表；实例输入单独存。
        self.instances: dict[str, list[str]] = {}
        self.instance_inputs: dict[str, Any] = {}
        self._instance_seq = 0
        # 被控制命令（Goto）显式激活的节点：显式 entry 时，只有入口和这里的节点
        # 能在“没有入边”的情况下起步，避免孤立节点被误当源点第一波就跑。
        self.activated: set[str] = set()

        self.state: RunState = RunState.RUNNING
        self.interrupt: Interrupt | None = None
        self.resume_value: Any = None
        self.final_output: Any = None
        self.metrics: dict[str, int] = {"waves": 0, "llm_calls": 0, "tool_calls": 0}

    # —— 对外便捷构造 ——
    @classmethod
    def start(cls, plan: Any, **kw: Any) -> Run:
        plan.validate()
        return cls(plan, **kw)

    # —— 状态机：唯一转移入口 ——
    def _transition(self, target: RunState) -> None:
        allowed = _ALLOWED_TRANSITIONS[self.state]
        if target not in allowed:
            raise RuntimeError(f"非法状态转移：{self.state} -> {target}")
        self.state = target

    def complete(self, output: Any = None) -> None:
        self.final_output = output
        self._transition(RunState.COMPLETED)

    def fail(self, reason: str) -> None:
        self.final_output = reason
        self._transition(RunState.FAILED)

    def suspend(self, interrupt: Interrupt) -> None:
        self.interrupt = interrupt
        self._transition(RunState.SUSPENDED)

    def resume(self, value: Any = None) -> None:
        self.resume_value = value
        self.interrupt = None
        self._transition(RunState.RUNNING)

    @property
    def running(self) -> bool:
        return self.state == RunState.RUNNING

    # —— 节点状态查询 ——
    def state_of(self, key: str) -> NodeRuntimeState:
        return self.node_states[key]

    @staticmethod
    def is_instance_key(key: str) -> bool:
        return "#" in key

    def is_instance(self, key: str) -> bool:
        return self.is_instance_key(key) and key in self.instance_inputs

    def template_of(self, key: str) -> str:
        return key.split("#", 1)[0] if self.is_instance_key(key) else key

    def is_pending(self, key: str) -> bool:
        st = self.node_states.get(key)
        return st is not None and st.status == NodeStatus.PENDING

    def is_completed(self, key: str) -> bool:
        st = self.node_states.get(key)
        return st is not None and st.status == NodeStatus.COMPLETED

    def is_terminal(self, key: str) -> bool:
        st = self.node_states.get(key)
        return st is not None and st.status in (
            NodeStatus.COMPLETED,
            NodeStatus.SKIPPED,
            NodeStatus.FAILED,
        )

    # —— 节点状态变更 ——
    def mark_running(self, key: str) -> None:
        self.node_states[key].mark_running()

    def mark_completed(self, key: str, output: Any) -> None:
        self.node_states[key].mark_completed(output)

    def mark_skipped(self, key: str) -> None:
        self.node_states[key].mark_skipped()

    def mark_failed(self, key: str, error: str) -> None:
        self.node_states[key].mark_failed(error)

    def reset_pending(self, key: str) -> None:
        self.node_states.setdefault(key, NodeRuntimeState()).reset_pending()
        self.activated.add(key)  # 记录“被命令激活”，供就绪判定放行

    # —— 动态实例（Send 扇出）——
    def add_instance(self, template: str, payload: Any, key: str | None = None) -> str:
        self._instance_seq += 1
        # 内部 key 统一带 “模板#” 前缀，这样凭 key 总能反推出它是哪个模板的实例。
        suffix = key if key is not None else str(self._instance_seq)
        full_key = f"{template}#{suffix}"
        if full_key not in self.node_states:
            self.node_states[full_key] = NodeRuntimeState()
            self.instance_inputs[full_key] = payload
            self.instances.setdefault(template, []).append(full_key)
        return full_key

    def input_of(self, key: str) -> Any:
        # 动态实例吃 Send 带来的 payload；静态节点吃上一步输出（由调度器另行传入）。
        return self.instance_inputs.get(key)

    # —— 波次屏障：按通道 reducer 折叠这一波的增量 ——
    def fold_writes(self, writes: list[Any], channels: dict[str, Channel]) -> dict[str, Any]:
        """把本波增量折叠进共享状态，返回“波增量”（供事件日志记录）。

        分两步，顺序不能反：先在波内把对同一通道的多次写入从中性元聚合成一个
        波增量，再把波增量折进历史状态。这样事件里存的是“这一波新增了什么”，
        重放整条事件流时逐波 fold，才能无重复地重建出最终状态。
        """
        wave_delta: dict[str, Any] = {}
        for w in writes:
            channel = channels[w.key]
            base = wave_delta.get(w.key, channel.empty)
            wave_delta[w.key] = channel.fold(base, w.value)
        for key, delta in wave_delta.items():
            channel = channels[key]
            self.shared[key] = channel.fold(self.shared.get(key, channel.init), delta)
        return wave_delta

    # —— 快照与恢复：只存数据，不存蓝图和活端口——
    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "task": self.task,
            "state": str(self.state),
            "shared": self.shared,
            "node_states": {
                k: {"status": str(v.status), "output": v.output, "attempts": v.attempts}
                for k, v in self.node_states.items()
            },
            "instances": self.instances,
            "instance_inputs": self.instance_inputs,
            "instance_seq": self._instance_seq,
            "activated": list(self.activated),
            "interrupt": None if self.interrupt is None else self.interrupt.__dict__,
            "final_output": self.final_output,
            "metrics": self.metrics,
        }

    @classmethod
    def restore(cls, plan: Any, snap: dict[str, Any]) -> Run:
        run = cls(
            plan,
            run_id=snap["run_id"],
            parent_id=snap.get("parent_id"),
            depth=snap.get("depth", 0),
            task=snap.get("task", ""),
        )
        run.shared = snap["shared"]
        run.node_states = {
            k: NodeRuntimeState(NodeStatus(v["status"]), v.get("output"), v.get("attempts", 0))
            for k, v in snap["node_states"].items()
        }
        run.instances = snap.get("instances", {})
        run.instance_inputs = snap.get("instance_inputs", {})
        run._instance_seq = snap.get("instance_seq", 0)
        run.activated = set(snap.get("activated", ()))
        run.final_output = snap.get("final_output")
        run.metrics = snap.get("metrics", run.metrics)
        # 恢复时直接落到当时的状态，绕过构造期的 RUNNING。
        run.state = RunState(snap["state"])
        if snap.get("interrupt"):
            d = snap["interrupt"]
            run.interrupt = Interrupt(
                d["kind"], d.get("payload"), d.get("question", ""), d.get("node_id", "")
            )
        return run
