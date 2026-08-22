"""FloorProjection — per-viewer filtering of the shared transcript.

Covers PublicTextOnly (strip tool_calls, truncate long text, speaker sees
their own turn verbatim) and SelectiveToolExposure (per-tool viewer
whitelist), plus project_floor's limit/ordering behavior.
"""

from __future__ import annotations

from prodagent.coordination.floor import FloorTurn, SharedFloor
from prodagent.coordination.floor_projection import (
    PublicTextOnly,
    SelectiveToolExposure,
    project_floor,
)
from prodagent.core.types import ToolCall


def _make_floor(turns: list[FloorTurn]) -> SharedFloor:
    floor = SharedFloor(session_id="s1")
    for speaker in {t.speaker for t in turns}:

        class _Stub:
            name = speaker

        floor.add_member(_Stub())
    floor.transcript.extend(turns)
    return floor


def test_public_text_only_strips_tool_calls_for_non_speaker_viewer():
    call = ToolCall(name="web_fetch", params={}, call_id="c1")
    turn = FloorTurn(speaker="alice", round=0, text="hello", tool_calls=[call], cost_usd=1.0)
    floor = _make_floor([turn])

    projected = project_floor(floor, viewer="bob", projection=PublicTextOnly())

    assert len(projected) == 1
    assert projected[0].tool_calls == []
    assert projected[0].cost_usd == 0.0
    assert projected[0].text == "hello"


def test_public_text_only_lets_speaker_see_their_own_turn_verbatim():
    call = ToolCall(name="web_fetch", params={}, call_id="c1")
    turn = FloorTurn(speaker="alice", round=0, text="hello", tool_calls=[call], cost_usd=1.0)
    floor = _make_floor([turn])

    projected = project_floor(floor, viewer="alice", projection=PublicTextOnly())

    assert projected[0].tool_calls == [call]
    assert projected[0].cost_usd == 1.0


def test_public_text_only_truncates_long_text_for_other_viewers():
    long_text = "x" * 5000
    turn = FloorTurn(speaker="alice", round=0, text=long_text)
    floor = _make_floor([turn])

    projected = project_floor(floor, viewer="bob", projection=PublicTextOnly(max_chars=100))

    assert len(projected[0].text) < 5000
    assert projected[0].text.startswith("x" * 100)
    assert "truncated" in projected[0].text
    # The speaker's own turn is untouched by the cap.
    own_view = project_floor(floor, viewer="alice", projection=PublicTextOnly(max_chars=100))
    assert own_view[0].text == long_text


def test_selective_tool_exposure_only_shows_whitelisted_tools_to_whitelisted_viewers():
    public_call = ToolCall(name="web_fetch", params={}, call_id="c1")
    private_call = ToolCall(name="read_private_notes", params={}, call_id="c2")
    turn = FloorTurn(speaker="alice", round=0, text="hi", tool_calls=[public_call, private_call])
    floor = _make_floor([turn])

    projection = SelectiveToolExposure(tool_visibility={"web_fetch": ["judge"]})

    judge_view = project_floor(floor, viewer="judge", projection=projection)
    assert judge_view[0].tool_calls == [public_call]

    bob_view = project_floor(floor, viewer="bob", projection=projection)
    assert bob_view[0].tool_calls == []


def test_selective_tool_exposure_default_denies_tools_absent_from_whitelist():
    call = ToolCall(name="undocumented_tool", params={}, call_id="c1")
    turn = FloorTurn(speaker="alice", round=0, text="hi", tool_calls=[call])
    floor = _make_floor([turn])

    projection = SelectiveToolExposure(tool_visibility={})
    projected = project_floor(floor, viewer="judge", projection=projection)

    assert projected[0].tool_calls == []


def test_project_floor_limit_caps_to_most_recent_turns_oldest_first():
    turns = [FloorTurn(speaker="alice", round=i, text=f"turn {i}") for i in range(5)]
    floor = _make_floor(turns)

    projected = project_floor(floor, viewer="alice", projection=PublicTextOnly(), limit=2)

    assert [t.text for t in projected] == ["turn 3", "turn 4"]


def test_project_floor_no_limit_returns_full_transcript():
    turns = [FloorTurn(speaker="alice", round=i, text=f"turn {i}") for i in range(3)]
    floor = _make_floor(turns)

    projected = project_floor(floor, viewer="alice", projection=PublicTextOnly())

    assert [t.text for t in projected] == ["turn 0", "turn 1", "turn 2"]
