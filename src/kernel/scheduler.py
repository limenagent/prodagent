"""scheduler —— 内核唯一的引擎：反复算“现在谁就绪”，一波一波推进。

主循环刻意写得很短，因为复杂度都被前面的部件吸收了：

    while 还在运行:
        ready   = plan.ready(run)           # 1) 沿边算这一波谁就绪
        results = 并发执行 ready            # 2) 波次并发（带上限），互不直接碰状态
        屏障: fold 增量 / 应用命令 / 落检查点  # 3) 一波结束才统一提交

三个关键性质：
- 波次是一致性边界：节点执行中只产出 Outcome，不直接写共享状态，
  全部在屏障处按 reducer 折叠，所以并发结果确定、且天然是一个提交点；
- 挂起是“放手”：某节点请求 Interrupt，会等本波其它节点跑完，再整体落盘暂停，
  resume 时只重跑当初那一个节点并把外部输入喂回去；
- 多 Agent 不新增引擎：SubPlanBody 通过激活端口递归跑一个子 Run，用的还是这里。
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from src.kernel.body import NodeContext, Outcome
from src.kernel.bus import Bus
from src.kernel.channels import WaveWrites
from src.kernel.command import Goto, Handoff, Send
from src.kernel.eventlog import (
    INTERRUPTED,
    NODE_COMPLETED,
    NODE_RETRY,
    NODE_STARTED,
    RESUMED,
    RUN_FAILED,
    RUN_STARTED,
    STATE_DELTA,
    Event,
    InMemoryEventLog,
    InMemoryStore,
)
from src.kernel.run import Run
from src.kernel.types import NodeStatus, RunState


class InProcessActivator:
    """默认子 Agent 激活器：在同进程用同一个调度器递归跑子 Plan（call 语义）。

    换远程实现（A2A、RPC）只需满足 SubagentPort 协议，内核一行不改——
    “在哪执行”是端口后面的事（位置透明）。
    """

    def __init__(self, scheduler: Scheduler):
        self.scheduler = scheduler

    async def activate(self, spec: Any, task: str, parent_run: Run, payload: Any = None) -> dict:
        child = Run.start(spec, parent_id=parent_run.run_id, depth=parent_run.depth + 1, task=task)
        await self.scheduler.drive(spec, child)
        return {
            "run_id": child.run_id,
            "state": str(child.state),
            "output": child.final_output,
            "shared": child.shared,
        }


class Scheduler:
    def __init__(
        self,
        *,
        llm: Any = None,
        tools: Any = None,
        bus: Bus | None = None,
        eventlog: Any = None,
        store: Any = None,
        on_handoff: Any = None,
        max_waves: int = 64,
        concurrency: int = 8,
    ):
        self.llm = llm
        self.tools = tools
        self.bus = bus or Bus()
        self.eventlog = eventlog or InMemoryEventLog()
        self.store = store or InMemoryStore()
        self.on_handoff = on_handoff  # transfer 语义是应用层配方，可注入
        self.max_waves = max_waves
        self._sem = asyncio.Semaphore(concurrency)
        self._seq: dict[str, int] = {}  # 每个 run 独立递增的事件序号
        self.subagent = InProcessActivator(self)

    # —— 对外主入口 ——
    async def run(self, plan: Any, *, task: str = "") -> Run:
        run = Run.start(plan, task=task)
        await self.drive(plan, run)
        return run

    async def resume(self, plan: Any, run_id: str, value: Any = None) -> Run:
        """从检查点恢复：取出挂起的节点，喂回外部值，只重跑它再继续。"""
        snap = await self.store.load(run_id)
        if snap is None:
            raise KeyError(f"找不到 {run_id} 的检查点，无法恢复")
        parked_node = (snap.get("interrupt") or {}).get("node_id", "")
        run = Run.restore(plan, snap)
        run.resume(value)
        if parked_node:
            run.reset_pending(parked_node)
        await self._emit(run, RESUMED, {"node": parked_node})
        await self.drive(plan, run)
        return run

    # —— 引擎主循环 ——
    async def drive(self, plan: Any, run: Run) -> None:
        if run.metrics["waves"] == 0 and run.state == RunState.RUNNING:
            await self._emit(run, RUN_STARTED, {"task": run.task})

        while run.running:
            ready = plan.ready(run)
            if not ready:
                # 先清理走不通的死分支（可能级联），再确认一次是否真的停摆。
                plan.sweep_skipped(run)
                ready = plan.ready(run)
            if not ready:
                self._settle(plan, run)
                break

            run.metrics["waves"] += 1
            if run.metrics["waves"] > self.max_waves:
                run.fail(f"超过最大波次 {self.max_waves}，疑似空转（请检查回边是否有进展）")
                await self._emit(run, RUN_FAILED, {"reason": run.final_output})
                break

            # 2) 波次并发：节点之间不共享可变状态，只各自产出 Outcome。
            results = await asyncio.gather(*[self._run_node(plan, run, key) for key in ready])

            # 3) 屏障：统一处理结果。任一节点失败，默认 fail-fast 让整 Run 停下。
            parked: tuple[str, Any] | None = None
            controls: list[tuple[str, Any]] = []
            writes = WaveWrites(plan.channels)

            for key, outcome, error in results:
                if error is not None:
                    run.mark_failed(key, repr(error))
                    run.fail(repr(error))
                    await self._emit(run, RUN_FAILED, {"node": key, "reason": repr(error)})
                    break
                if outcome.suspend is not None:
                    parked = (key, outcome.suspend)
                    continue
                run.mark_completed(key, outcome.value)
                for k, v in outcome.state_delta.items():
                    writes.buffer(k, v, key)
                if outcome.control is not None:
                    controls.append((key, outcome.control))
                await self._emit(run, NODE_COMPLETED, {"node": key})

            if run.state == RunState.FAILED:
                break

            writes.check_ambiguous()
            folded = run.fold_writes(writes.drain(), plan.channels)
            if folded:
                await self._emit(run, STATE_DELTA, {"delta": folded})

            await self._apply_controls(plan, run, controls)

            if parked is not None:
                key, interrupt = parked
                interrupt = dataclasses.replace(interrupt, node_id=key)
                run.suspend(interrupt)
                await self._emit(run, INTERRUPTED, {"node": key, "question": interrupt.question})
                await self._checkpoint(run)
                break

            await self._checkpoint(run)

    # —— 单个节点的执行 ——
    async def _run_node(
        self, plan: Any, run: Run, key: str
    ) -> tuple[str, Outcome | None, BaseException | None]:
        template = run.template_of(key)
        node = plan.get(template)
        run.mark_running(key)
        await self._emit(run, NODE_STARTED, {"node": key})
        ctx = NodeContext(
            run,
            key,
            llm=self.llm,
            tools=self.tools,
            subagent=self.subagent,
            bus=self.bus,
            resume_value=run.resume_value,
        )
        try:
            async with self._sem:  # 全局并发上限
                outcome = await self._run_body(
                    node, self._node_input(plan, run, key), ctx, run, key
                )
            return key, outcome, None
        except BaseException as exc:  # 交回屏障统一处置
            return key, None, exc

    async def _run_body(
        self, node: Any, value: Any, ctx: NodeContext, run: Run, key: str
    ) -> Outcome:
        """执行一个节点的 body，套上“超时 + 重试退避”这层步骤级弹性。

        机制是固定的：超时算一次失败、失败按策略决定是否再来一次；策略本身
        （试几次、等多久、哪些错值得重试）挂在 Node 上，可整体替换。外部取消
        不属于“可重试的失败”，必须原样向上传播。
        """
        policy = node.retry
        attempts = policy.max_attempts if policy else 1
        last_exc: BaseException | None = None
        for attempt in range(attempts):
            try:
                if node.timeout is not None:
                    return await asyncio.wait_for(node.body.run(value, ctx), node.timeout)
                return await node.body.run(value, ctx)
            except asyncio.CancelledError:
                raise  # 被外部取消：立即停，不重试
            except BaseException as exc:
                last_exc = exc
                can_retry = (
                    policy is not None
                    and attempt + 1 < attempts
                    and isinstance(exc, policy.retry_on)
                )
                if not can_retry:
                    raise
                backoff = policy.delay_for(attempt)
                await self._emit(
                    run,
                    NODE_RETRY,
                    {"node": key, "attempt": attempt + 1, "error": repr(exc), "backoff": backoff},
                )
                await asyncio.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    def _node_input(self, plan: Any, run: Run, key: str) -> Any:
        if run.is_instance(key):
            return run.input_of(key)
        preds = plan.incoming(key)
        if not preds:
            return run.task
        upstream: dict[str, Any] = {}
        for e in preds:
            src = e.source
            node = plan.get(src)
            if node.template:  # 模板前驱：聚合其所有实例输出
                vals = [
                    run.state_of(k).output
                    for k in run.instances.get(src, ())
                    if run.is_completed(k)
                ]
                if vals:
                    upstream[src] = vals
            elif run.is_completed(src) and plan._edge_live(e, run.shared):
                # 只沿“此刻活着”的边取前驱输出：条件边没选中的分支不喂输入，
                # 否则互斥分支里没走的那个前驱会把输入拼成多余的 dict。
                upstream[src] = run.state_of(src).output
        if len(upstream) == 1:
            return next(iter(upstream.values()))
        return upstream

    # —— 控制命令：改的是“下一波的就绪集合” ——
    async def _apply_controls(self, plan: Any, run: Run, controls: list[tuple[str, Any]]) -> None:
        for _writer, control in controls:
            commands = control if isinstance(control, list) else [control]
            for cmd in commands:
                if isinstance(cmd, Goto):
                    run.reset_pending(cmd.target)  # 回边/跳转：目标重新就绪
                elif isinstance(cmd, Send):
                    run.add_instance(cmd.template, cmd.payload, cmd.key)
                elif isinstance(cmd, Handoff):
                    # transfer（不回头的交接）涉及会话归属，是应用层配方。
                    if self.on_handoff is None:
                        raise RuntimeError(
                            "收到 Handoff 但未装配 on_handoff；委派请用 SubPlanBody(call)，"
                            "交接(transfer)需在应用层提供处理器。"
                        )
                    await self.on_handoff(cmd, run, self)
                else:
                    raise TypeError(f"未知控制命令：{cmd!r}")

    # —— 收尾 ——
    def _settle(self, plan: Any, run: Run) -> None:
        if plan.is_done(run):
            run.complete(self._final_output(plan, run))
        else:
            pending = [
                k
                for k, s in run.node_states.items()
                if s.status == NodeStatus.PENDING
                and not (k in plan.nodes and plan.nodes[k].template)
            ]
            run.fail(f"图停滞：没有就绪节点但仍有未完成节点 {pending}（多半是边没连对）")

    def _final_output(self, plan: Any, run: Run) -> Any:
        # 只取真正完成的汇聚节点，被条件边结构性跳过的分支不进最终结果。
        terms = [t for t in plan.terminal_ids() if run.is_completed(t)]
        values = {t: run.state_of(t).output for t in terms}
        if len(values) == 1:
            return next(iter(values.values()))
        return values

    # —— 事件与检查点 ——
    async def _emit(self, run: Run, kind: str, data: dict | None = None) -> None:
        seq = self._seq.get(run.run_id, 0) + 1
        self._seq[run.run_id] = seq
        event = Event(seq, run.run_id, kind, data or {}, parent_id=run.parent_id)
        await self.eventlog.append(event)
        await self.bus.fire(kind, evt=event)

    async def _checkpoint(self, run: Run) -> None:
        await self.store.save(run.run_id, run.snapshot())
