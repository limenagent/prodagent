"""Resume from checkpoint — state lands on disk, and a brand-new instance
resumes from the suspension point.

The first run suspends at "waiting for a human"; the checkpoint and the event
log are already written to the directory. We then **rebuild** a Workflow
(simulating a process restart, or even another machine mounting the same
directory): it holds no reference to the previous object's memory and
continues from the on-disk checkpoint alone.

Run: PYTHONPATH=. python3 examples/persistence.py
"""

import asyncio
import os
import tempfile

from src import Workflow, go, wait_human
from src.backends.file_store import FileCheckpointStore, FileEventLog


def build(directory: str) -> Workflow:
    """Returns a brand-new instance every time; they share one disk directory."""
    wf = Workflow(store=FileCheckpointStore(directory), eventlog=FileEventLog(directory))

    async def prepare(x, ctx):
        return "Refund request prepared, amount 88"

    async def approve(prep, ctx):
        if ctx.resume_value is None:
            return wait_human("Approve this 88 refund?", {"prep": prep})
        return go(
            "finish", prep, decision="refunded" if ctx.resume_value.get("approved") else "revoked"
        )

    async def finish(prep, ctx):
        return f"{prep} | action taken: {ctx.shared['decision']}"

    wf.add("prepare", prepare)
    wf.add("approve", approve)
    wf.add("finish", finish, terminal=True)
    wf.edge("prepare", "approve")
    wf.edge("approve", "finish")
    wf.entry("prepare")
    return wf


async def main():
    directory = tempfile.mkdtemp(prefix="src-ckpt-")

    first = build(directory)  # the first instance (think: the live process)
    r1 = await first.run("Order O-1 requests a refund")
    print("First run:", r1.status)
    print("Files on disk:", sorted(os.listdir(directory)))

    second = build(directory)  # a brand-new instance (think: the process after a restart)
    r2 = await second.resume(r1.run_id, {"approved": True})
    print("Fresh instance resumed from disk:", r2.status, "|", r2.output)


if __name__ == "__main__":
    asyncio.run(main())
