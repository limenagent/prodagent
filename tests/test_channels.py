"""通道与 reducer：并发写入不丢数，last 通道同波多写要明确报错。"""

import pytest

from src.kernel import AmbiguousWrite, WaveWrites, add, append, last, merge


def test_reducers_fold():
    assert append().fold(["a"], ["b", "c"]) == ["a", "b", "c"]
    assert add(0).fold(1, 2) == 3
    assert last().fold("old", "new") == "new"
    assert merge().fold({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


def test_wavewrites_collects_in_declared_channels():
    w = WaveWrites({"items": append(), "n": add(0)})
    w.buffer("items", [1], "b1")
    w.buffer("items", [2], "b2")
    w.buffer("n", 1, "b1")
    w.check_ambiguous()
    folded = {}
    for write in w.drain():
        ch = {"items": append(), "n": add(0)}[write.key]
        folded[write.key] = ch.fold(folded.get(write.key), write.value)
    assert folded["items"] == [1, 2]
    assert folded["n"] == 1


def test_undeclared_channel_rejected():
    with pytest.raises(KeyError):
        WaveWrites({"a": append()}).buffer("b", 1, "x")


def test_last_channel_ambiguous_in_one_wave():
    w = WaveWrites({"v": last()})
    w.buffer("v", 1, "a")
    w.buffer("v", 2, "b")
    with pytest.raises(AmbiguousWrite):
        w.check_ambiguous()
