"""FileGraphStore: the file is the source of truth across store instances."""

from __future__ import annotations

from prodagent.backends.file.graph import FileGraphStore


async def test_cross_instance_visibility(tmp_path) -> None:
    a = FileGraphStore(tmp_path)
    b = FileGraphStore(tmp_path)

    await a.add_node("n1", labels=["Fact"], properties={"v": 1})
    assert await b.get_node("n1") is not None, "second store must see the first's write"

    await b.add_node("n2")
    names = {n["id"] for n in await a.list_nodes()}
    assert names == {"n1", "n2"}, "a's rewrite must not erase b's node"
