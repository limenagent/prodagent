"""Artifacts: bytes live in the BlobStore, pointer facts in the event stream,
and the artifact library is a projection — the same law as state.
"""

from __future__ import annotations

from src.kernel import (
    FnBody,
    InMemoryBlobStore,
    Node,
    Plan,
    Scheduler,
    artifacts_from_events,
    latest_artifacts,
)


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
