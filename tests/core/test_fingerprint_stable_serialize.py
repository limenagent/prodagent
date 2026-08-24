from __future__ import annotations

import datetime
from pathlib import Path

from prodagent.core.progress import _tool_fingerprint
from prodagent.kernel.types import ToolCall, stable_serialize


class TestStableSerializeNoCollisions:
    def test_datetime_params_do_not_collide_with_strings(self):
        a = ToolCall(
            name="t",
            params={"at": datetime.datetime(2024, 1, 1, 0, 0, 0)},
            call_id="c",
        )
        b = ToolCall(
            name="t",
            params={"at": "2024-01-01 00:00:00"},
            call_id="c",
        )
        assert _tool_fingerprint(a) != _tool_fingerprint(b)
        assert a.params_hash != b.params_hash

    def test_two_datetimes_with_different_values_do_not_collide(self):
        a = ToolCall(
            name="t",
            params={"at": datetime.datetime(2024, 1, 1)},
            call_id="c",
        )
        b = ToolCall(
            name="t",
            params={"at": datetime.datetime(2024, 1, 2)},
            call_id="c",
        )
        assert _tool_fingerprint(a) != _tool_fingerprint(b)

    def test_lambda_params_do_not_silently_collide(self):
        a = ToolCall(name="t", params={"fn": (lambda: 1)}, call_id="c")
        b = ToolCall(name="t", params={"fn": (lambda: 2)}, call_id="c")
        assert _tool_fingerprint(a) != _tool_fingerprint(b)

    def test_path_params_keep_canonical_form(self):
        a = ToolCall(name="t", params={"p": Path("/tmp/a")}, call_id="c")
        b = ToolCall(name="t", params={"p": Path("/tmp/b")}, call_id="c")
        assert _tool_fingerprint(a) != _tool_fingerprint(b)

    def test_stable_serialize_datetime_isoformat(self):
        assert stable_serialize(datetime.datetime(2024, 1, 1)) == "2024-01-01T00:00:00"

    def test_stable_serialize_unknown_object_uses_repr(self):
        class Custom:
            def __repr__(self):
                return "Custom(repr)"

        assert stable_serialize(Custom()) == "Custom(repr)"
