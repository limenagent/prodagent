"""Resume from the event log alone — the .jsonl IS the database.

The first run suspends at "waiting for a human" and every fact lands in one
append-only event log on disk (no snapshot, no side table). We then **rebuild**
a Workflow in a brand-new instance (simulating a process restart, or even
another machine mounting the same directory): it holds no reference to the
previous process's memory and continues purely by replaying the log and feeding
the answer back.

Run: PYTHONPATH=. python3 examples/persistence.py
"""

import asyncio
import os
import tempfile

from src import Workflow, go, wait_human
from src.backends.file_store import FileEventLog


def build(directory: str) -> Workflow:
    """Returns a brand-new instance every time; they share one log directory."""
    wf = Workflow()
    # A Workflow is a pure definition: the durable ledger is injected when it
    # is hosted, not carried by the constructor — and the ledger is one thing,
    # the event log.
    wf.host(eventlog=FileEventLog(directory))

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

    wf.add_node("prepare", prepare)
    wf.add_node("approve", approve)
    wf.add_node("finish", finish, terminal=True)
    wf.add_edge("prepare", "approve")
    wf.add_edge("approve", "finish")
    wf.entry("prepare")
    return wf


async def main():
    directory = tempfile.mkdtemp(prefix="prodagent-log-")

    first = build(directory)  # the first instance (think: the live process)
    r1 = await first.run("Order O-1 requests a refund")
    print("First run:", r1.status)
    print("Files on disk:", sorted(os.listdir(directory)))

    second = build(directory)  # a brand-new instance (think: the process after a restart)
    # resume = replay(the log) + feed(the answer); nothing else survives the crash
    r2 = await second.resume(r1.run_id, {"approved": True})
    print("Fresh instance resumed from the log:", r2.status, "|", r2.output)


if __name__ == "__main__":
    asyncio.run(main())
