"""Scopes — the five-level ladder and the controlled write door (column 9)."""

from __future__ import annotations

import pytest

from prodagent.base.scopes import Scope, ScopeError, ScopeView


def _view(**kwargs) -> ScopeView:
    layers: dict = dict(
        run={"scratch": "r"},
        session={"spine": "s", "shared_with_run": "from-session"},
        user={"name": "u"},
        app={"kb": "a"},
    )
    layers.update(kwargs)
    return ScopeView(**layers)


def test_reads_fall_through_inner_to_outer():
    view = _view()
    assert view.get("scratch") == "r"
    assert view.get("spine") == "s"
    assert view.get("name") == "u"
    assert view.get("kb") == "a"
    assert view.get("missing", "dflt") == "dflt"


def test_inner_layer_shadows_an_outer_key_of_the_same_name():
    assert _view().get("shared_with_run") == "from-session"
    assert _view(run={"shared_with_run": "from-run"}).get("shared_with_run") == "from-run"


def test_a_named_scope_read_skips_the_ladder():
    view = _view()
    assert view.get("spine", scope=Scope.SESSION) == "s"
    assert view.get("spine", scope=Scope.RUN) is None


def test_run_layer_writes_by_default():
    run: dict = {"scratch": "r"}
    ScopeView(run=run).put("answer", 42)
    assert run["answer"] == 42


def test_outer_scope_write_fails_closed_without_a_grant():
    view = _view()
    with pytest.raises(ScopeError, match="read-only"):
        view.put("memory", "m", scope=Scope.SESSION)
    with pytest.raises(ScopeError):
        view.put("profile", "p", scope=Scope.USER)


def test_an_explicit_grant_opens_the_outer_scope():
    session: dict = {"spine": "s"}
    view = ScopeView(session=session, writable=frozenset({Scope.RUN, Scope.SESSION}))
    view.put("note", "n", scope=Scope.SESSION)
    assert session["note"] == "n"


def test_temp_rides_beside_the_ladder():
    temp: dict = {}
    view = ScopeView(temp=temp)
    view.put("draft", "d", scope=Scope.TEMP)
    assert temp == {"draft": "d"}
    assert view.get("draft") == "d"


def test_all_five_levels_exist_with_the_column_names():
    assert [s.value for s in Scope] == ["app", "user", "session", "run", "temp"]
