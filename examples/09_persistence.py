"""09 断点续跑 —— 状态落在磁盘，换一个全新实例也能从挂起点恢复。

第一次运行在“等待人工”处挂起，检查点与事件日志已写入目录；随后我们**重新构建**
一个 Workflow（模拟进程重启、甚至换一台机器挂同一个目录），它不依赖上一个对象
的内存，只凭磁盘上的检查点就从断点继续。

跑法：PYTHONPATH=. python3 examples/09_persistence.py
"""

import asyncio
import os
import tempfile

from src import Workflow, go, wait_human
from src.backends.file_store import FileCheckpointStore, FileEventLog


def build(directory: str) -> Workflow:
    """每次都返回一个全新实例，但它们共用同一个磁盘目录。"""
    wf = Workflow(store=FileCheckpointStore(directory), eventlog=FileEventLog(directory))

    async def prepare(x, ctx):
        return "退款单已准备好，金额 88 元"

    async def approve(prep, ctx):
        if ctx.resume_value is None:
            return wait_human("批准这笔 88 元退款吗？", {"prep": prep})
        return go(
            "finish", prep, decision="已退款" if ctx.resume_value.get("approved") else "已撤销"
        )

    async def finish(prep, ctx):
        return f"{prep}｜处置：{ctx.shared['decision']}"

    wf.add("prepare", prepare)
    wf.add("approve", approve)
    wf.add("finish", finish, terminal=True)
    wf.edge("prepare", "approve")
    wf.edge("approve", "finish")
    wf.entry("prepare")
    return wf


async def main():
    directory = tempfile.mkdtemp(prefix="src-ckpt-")

    first = build(directory)  # 第一个实例（可理解为上线进程）
    r1 = await first.run("订单 O-1 申请退款")
    print("第一次运行：", r1.status)
    print("磁盘文件：", sorted(os.listdir(directory)))

    second = build(directory)  # 全新实例（可理解为重启后的进程）
    r2 = await second.resume(r1.run_id, {"approved": True})
    print("新实例从磁盘恢复后：", r2.status, "｜", r2.output)


if __name__ == "__main__":
    asyncio.run(main())
