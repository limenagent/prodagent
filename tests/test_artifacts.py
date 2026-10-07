"""Artifacts: bytes live in the BlobStore, pointer facts in the event stream,
and the artifact library is a projection — the same law as state.
"""

from __future__ import annotations

import pytest

from src.kernel import (
    FnBody,
    InMemoryBlobStore,
    Node,
    Outcome,
    Plan,
    Scheduler,
    artifacts_from_events,
    latest_artifacts,
)
from src.kernel.eventlog import ARTIFACT_WRITTEN, Event


def _plan_with(fn, name="w"):
    plan = Plan(name="writer")
    plan.add(Node(name, FnBody(fn), terminal=True))
    plan.entry = (name,)
    return plan


async def test_artifact_saved_and_projected():
    async def write_report(_input, ctx):
        pointer = await ctx.save_artifact("report.md", "# Hello\n\nworld", title="Report")
        return f"saved v{pointer['version']}"

    sched = Scheduler()
    run = await sched.run(_plan_with(write_report))
    assert run.final_output == "saved v1"

    events = await sched.eventlog.events(run.run_id)
    latest = latest_artifacts(events)
    assert set(latest) == {"report.md"}
    pointer = latest["report.md"]
    assert pointer["version"] == 1
    assert pointer["mime"] == "text/markdown"  # inferred from the .md extension
    assert pointer["size"] == len(b"# Hello\n\nworld")
    assert pointer["title"] == "Report"

    # The bytes round-trip through the BlobStore, not the event stream.
    assert await sched.blobs.load(pointer["uri"]) == b"# Hello\n\nworld"


async def test_artifact_versions_increment():
    async def write_twice(_input, ctx):
        await ctx.save_artifact("f.txt", "one")
        await ctx.save_artifact("f.txt", "two")
        return "ok"

    sched = Scheduler()
    run = await sched.run(_plan_with(write_twice))
    events = await sched.eventlog.events(run.run_id)
    versions = artifacts_from_events(events)["f.txt"]
    assert [v["version"] for v in versions] == [1, 2]
    assert await sched.blobs.load(versions[0]["uri"]) == b"one"
    assert await sched.blobs.load(versions[1]["uri"]) == b"two"
    # latest view points at v2
    assert latest_artifacts(events)["f.txt"]["version"] == 2


async def test_dict_artifact_serialized_as_json():
    async def write_data(_input, ctx):
        await ctx.save_artifact("data.json", {"a": 1, "b": [2, 3]})
        return "ok"

    sched = Scheduler()
    run = await sched.run(_plan_with(write_data))
    pointer = latest_artifacts(await sched.eventlog.events(run.run_id))["data.json"]
    assert pointer["mime"] == "application/json"
    assert b'"a": 1' in await sched.blobs.load(pointer["uri"])


async def test_custom_blob_store_is_used():
    # A caller-supplied BlobStore receives the bytes (dependency injection).
    async def write(_input, ctx):
        await ctx.save_artifact("x.txt", "hi")
        return "ok"

    blobs = InMemoryBlobStore()
    sched = Scheduler(blobs=blobs)
    run = await sched.run(_plan_with(write))
    pointer = latest_artifacts(await sched.eventlog.events(run.run_id))["x.txt"]
    assert await blobs.load(pointer["uri"]) == b"hi"


async def test_version_gap_after_delete_never_overwrites(tmp_path):
    # next_version is max(present)+1, never a count: deleting a middle version
    # must not make the next save collide with — and overwrite — a later one.
    from src.backends.blob_store import LocalBlobStore

    blobs = InMemoryBlobStore()
    for text in ("one", "two", "three"):
        await blobs.save("r", "f.txt", text)
    await blobs.delete("r/f.txt.v2")  # leave a gap
    assert (await blobs.save("r", "f.txt", "four"))["version"] == 4  # count+1 = 3
    assert await blobs.load("r/f.txt.v3") == b"three"  # untouched

    # the durable local-directory store shares the same rule (one next_version)
    local = LocalBlobStore(str(tmp_path))
    for text in ("one", "two", "three"):
        await local.save("r", "f.txt", text)
    await local.delete("r/f.txt.v2")
    assert (await local.save("r", "f.txt", "four"))["version"] == 4
    assert await local.load("r/f.txt.v3") == b"three"


async def test_local_store_clamps_uris_to_its_root(tmp_path):
    """Security, not tidiness: load/delete take uris straight off the wire in
    the playground, so "../" or an absolute path must never address outside the
    store's root directory."""
    import pytest

    from src.backends.blob_store import LocalBlobStore

    local = LocalBlobStore(str(tmp_path))
    await local.save("r", "f.txt", "x")
    for evil in ("../../etc/passwd", "/etc/passwd", "r/../../../etc/passwd"):
        with pytest.raises(ValueError):
            await local.load(evil)
        with pytest.raises(ValueError):
            await local.delete(evil)


async def test_artifact_read_back_across_nodes():
    """Save in one node, load in the next: the pointer folds from this run's
    facts (the stream is the only index) and the bytes come from the store."""

    async def save(_, ctx):
        await ctx.save_artifact("report.md", "# Title\n\nbody")
        return "saved"

    async def load(_, ctx):
        return (await ctx.load_artifact("report.md")).decode().splitlines()[0]

    plan = Plan(name="roundtrip")
    plan.add(Node("save", FnBody(save)))
    plan.add(Node("load", FnBody(load), terminal=True))
    plan.entry = ("save",)
    plan.edge("save", "load")
    run = await Scheduler().run(plan)
    assert run.final_output == "# Title"


async def test_load_artifact_pins_versions_and_reports_missing():
    import pytest

    async def rw(_, ctx):
        await ctx.save_artifact("f.txt", "one")
        await ctx.save_artifact("f.txt", "two")
        latest = await ctx.load_artifact("f.txt")
        pinned = await ctx.load_artifact("f.txt", version=1)
        with pytest.raises(FileNotFoundError):
            await ctx.load_artifact("f.txt", version=9)
        with pytest.raises(FileNotFoundError):
            await ctx.load_artifact("missing.txt")
        return (latest + b"/" + pinned).decode()

    run = await Scheduler().run(_plan_with(rw))
    assert run.final_output == "two/one"


async def test_tool_reads_an_artifact_back():
    """Tools receive the same NodeContext, so "one tool fetches and saves, a
    later tool greps the bytes" composes with no new plumbing."""
    from src.runtime.tools import ToolRegistry

    async def fetch(name, ctx):
        await ctx.save_artifact("logs.txt", "line1 ERROR boom\nline2 ok")
        return f"saved logs.txt for {name}"

    async def grep(pattern, ctx):
        text = (await ctx.load_artifact("logs.txt")).decode()
        return "\n".join(line for line in text.splitlines() if pattern in line)

    tools = ToolRegistry()
    tools.function(fetch)
    tools.function(grep)

    async def detective(_, ctx):
        await ctx.call_tool("fetch", {"name": "payments"})
        return await ctx.call_tool("grep", {"pattern": "ERROR"})

    run = await Scheduler(tools=tools).run(_plan_with(detective))
    assert run.final_output.output == "line1 ERROR boom"


async def test_read_back_survives_resume():
    """The bytes were saved before the suspension; resume replays the facts
    and continues, and the reader folds the pointer from the rebuilt stream —
    there is no second store to lose."""

    async def draft(_, ctx):
        if ctx.resume_value is None:
            await ctx.save_artifact("draft.md", "draft words")
            return Outcome.park("review")
        return ctx.resume_value

    async def polish(_, ctx):
        return (await ctx.load_artifact("draft.md")).decode().upper()

    plan = Plan(name="resume-read")
    plan.add(Node("draft", FnBody(draft)))
    plan.add(Node("polish", FnBody(polish), terminal=True))
    plan.entry = ("draft",)
    plan.edge("draft", "polish")
    sched = Scheduler()
    run = await sched.run(plan)
    assert run.state.value == "suspended"
    run = await sched.resume(plan, run.run_id, "approved")
    assert run.final_output == "DRAFT WORDS"


def _written(seq: int, filename: str, version: int) -> Event:
    return Event(
        seq,
        "run-x",
        ARTIFACT_WRITTEN,
        {"filename": filename, "version": version, "uri": f"u{seq}", "mime": "", "size": 1},
    )


def test_fold_refuses_regressed_versions():
    """Store-assigned numbers arriving out of commit order (two same-wave
    writers racing one filename — unreachable on the in-memory teaching port,
    which has no yield point between save and record) would make "latest"
    lie; the fold raises instead of blessing it. Placeholder law: once
    versions are stamped in the fold (the Run-tree change), this is a plain
    regression test."""
    events = [_written(3, "f.txt", 2), _written(4, "f.txt", 1)]
    with pytest.raises(ValueError, match="strictly increase"):
        artifacts_from_events(events)


def test_fold_allows_sparse_versions():
    """A crash between the bytes landing and the fact committing skips a
    number — sparse but increasing is healthy history, not divergence."""
    events = [_written(3, "f.txt", 1), _written(4, "f.txt", 3)]
    folded = artifacts_from_events(events)["f.txt"]
    assert [p["version"] for p in folded] == [1, 3]
    assert latest_artifacts(events)["f.txt"]["version"] == 3
