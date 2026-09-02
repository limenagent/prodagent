"""Settlement convergence — the shared atoms extracted in the G0 pre-work.

Pins three contracts:

- :func:`prodagent.kernel.budget.run_enveloped` — the one reserve→act→commit
  envelope (spawn and the stage drivers both delegate here; the crash policy
  must not be able to drift between them);
- :func:`prodagent.kernel.budget.hop_own_share` — the hop's own
  spend, children excluded, for the relay's settle-at-boundary commit;
- the relay's checkpoint save going through ``save_and_fire_checkpoint`` —
  a failing relay save must fire CHECKPOINT_FAILED like every other save
  path (it used to call ``store.save`` directly and fail silently);
- ``AgentRun`` serialization schema versioning.
"""

from __future__ import annotations

import pytest

import prodagent.backends.file.checkpoint as checkpoint_module
from prodagent import Agent, AgentConfig, ExecutionMode
from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.kernel.budget import (
    BudgetLedger,
    HardBudget,
    SpawnAccumulator,
    hop_own_share,
    run_enveloped,
)
from prodagent.kernel.bus import HookEvent, HookRegistry
from prodagent.kernel.state import AgentRun
from prodagent.llm.fake import script


def _ledger(max_turns: int = 5) -> BudgetLedger:
    return BudgetLedger(max=HardBudget(max_turns=max_turns, max_seconds=600))


# --------------------------------------------------------------- run_enveloped


async def test_envelope_reserves_then_commits_actuals():
    ledger = _ledger()

    settled = await run_enveloped(ledger, member="alice", act=returning((2, 30, 0.25)))

    assert settled == (2, 30, 0.25)
    assert ledger.committed.turns == 2
    assert ledger.committed.tokens == 30
    assert ledger.committed.cost_usd == pytest.approx(0.25)
    assert ledger.spent.turns == 2  # reservation reconciled away, not stacked


async def test_envelope_rejects_member_that_cannot_reserve_without_acting():
    ledger = _ledger(max_turns=1)
    await ledger.commit(member="bob", turns=1, tokens=0, cost_usd=0.0)

    acted = False

    async def _act() -> tuple[int, int, float]:
        nonlocal acted
        acted = True
        return (1, 0, 0.0)

    settled = await run_enveloped(ledger, member="alice", act=_act)

    assert settled is None
    assert acted is False  # over-cap members never run their unit
    assert ledger.committed.turns == 1  # no budget movement for alice


async def test_envelope_act_returning_none_still_spends_the_turn_slot():
    ledger = _ledger()

    async def _nothing() -> None:
        return None

    settled = await run_enveloped(ledger, member="alice", act=_nothing)

    assert settled is None
    assert ledger.committed.turns == 1
    assert ledger.committed.tokens == 0


async def test_envelope_crash_commits_the_turn_and_reraises():
    ledger = _ledger()

    async def _boom() -> tuple[int, int, float]:
        raise RuntimeError("child exploded")

    with pytest.raises(RuntimeError):
        await run_enveloped(ledger, member="alice", act=_boom)

    # Crash-commits-don't-release: a crash-looping member stays visible on
    # the turns axis instead of leaking an eternal reservation.
    assert ledger.committed.turns == 1
    assert ledger.spent.turns == 1
    assert ledger.member_reserved("alice").turns == 0


async def test_envelope_without_ledger_runs_the_act_bare():
    settled = await run_enveloped(None, member="alice", act=returning((3, 10, 0.1)))
    assert settled == (3, 10, 0.1)


def returning(actuals: tuple[int, int, float]):
    """A ready-to-await act closure that settles ``actuals``."""

    async def _act() -> tuple[int, int, float]:
        return actuals

    return _act


# ---------------------------------------------------------------- hop_own_share


def test_hop_own_share_subtracts_children_folded_into_run_totals():
    run = AgentRun(run_id="r", task="t")
    run.metrics.turn_count = 6
    run.metrics.input_tokens = 100
    run.metrics.output_tokens = 40
    run.metrics.cost_usd = 1.5
    acc = SpawnAccumulator(cost_usd=0.5, turns=2, input_tokens=30, output_tokens=10, spawn_count=1)

    turns, tokens, cost = hop_own_share(run, acc)

    assert turns == 4
    assert tokens == 100  # (100 + 40) − (30 + 10)
    assert cost == pytest.approx(1.0)


def test_hop_own_share_clamps_at_zero_and_tolerates_missing_accumulator():
    run = AgentRun(run_id="r", task="t")
    run.metrics.turn_count = 1
    run.metrics.input_tokens = 5
    run.metrics.output_tokens = 5
    acc = SpawnAccumulator(cost_usd=2.0, turns=9, input_tokens=99, output_tokens=99)
    acc.spawn_count = 1

    assert hop_own_share(run, acc) == (0, 0, 0.0)

    bare = AgentRun(run_id="r2", task="t")
    bare.metrics.turn_count = 2
    bare.metrics.input_tokens = 7
    bare.metrics.output_tokens = 3
    assert hop_own_share(bare, None) == (2, 10, 0.0)


# ------------------------------------------- relay save path fires the failure hook


async def test_relay_checkpoint_failure_fires_checkpoint_failed(tmp_path, monkeypatch):
    """The relay's post-handoff save goes through ``save_and_fire_checkpoint``:
    a failing write flips the sticky flag and fires CHECKPOINT_FAILED instead
    of failing silently (the pre-fix code called ``store.save`` directly)."""

    real_write = checkpoint_module.write_atomic_json
    write_counts: dict[str, int] = {}

    def _flaky_second_write(path, payload, **kw):
        # Checkpoints are versioned per file (run.v1.json, run.v2.json …) —
        # count per run, not per path, so "second save of run A" (the relay's)
        # is the one that fails.
        key = str(path).rsplit(".v", 1)[0]
        write_counts[key] = write_counts.get(key, 0) + 1
        if write_counts[key] >= 2:
            raise OSError("simulated relay save failure")
        return real_write(path, payload, **kw)

    monkeypatch.setattr(checkpoint_module, "write_atomic_json", _flaky_second_write)

    hooks = HookRegistry()
    failures: list[dict] = []
    hooks.register_event(HookEvent.CHECKPOINT_FAILED, lambda **kw: failures.append(kw))

    peer_b = Agent("B", system_prompt="you are B", mode=ExecutionMode.REACTIVE)
    peer_b.config.llm = script({"content": "B done"})
    peer_b.config.hooks = hooks
    agent_a = Agent(
        "A",
        system_prompt="you are A",
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name="A", peers=[peer_b], checkpoint=FileCheckpointStore(directory=tmp_path)
        ),
    )
    agent_a.config.llm = script({"tool": "handoff_to_B", "params": {"task": "go"}})
    agent_a.config.hooks = hooks

    run = await agent_a.chat("start", session_id="relay-save-failure")

    assert run.state.value == "completed"  # save failure degrades, never kills the chain
    assert failures, "relay save failure must fire CHECKPOINT_FAILED"
    assert any(f.get("run_id", "").startswith("relay-save-failure") for f in failures)


# ---------------------------------------------------------------- schema version


def test_run_dict_carries_schema_version_and_round_trips():
    run = AgentRun(run_id="r", task="t")
    d = run.to_dict()
    assert d["schema_version"] == 2

    restored = AgentRun.from_dict(d)
    assert restored.to_dict()["schema_version"] == 2


def test_from_dict_tolerates_legacy_checkpoint_without_schema_version():
    legacy = AgentRun(run_id="r", task="t").to_dict()
    del legacy["schema_version"]

    restored = AgentRun.from_dict(legacy)
    assert restored.run_id == "r"
    assert restored.to_dict()["schema_version"] == 2


def test_v1_checkpoint_with_flat_cursor_fields_migrates_into_boxed_cursors():
    """v1 wrote plan tails flat on the run dict; v2 boxes them. A v1
    checkpoint must load and migrate (data-model unit 2's compat promise)."""
    v1 = {
        "run_id": "legacy",
        "task": "t",
        "schema_version": 1,
        "plan_state": {"version": 2, "nodes": {"s1": {"status": "completed"}}},
        "plan_last_seq": 5,
        "last_event_seq": 9,
    }
    run = AgentRun.from_dict(v1)
    assert run.cursor("plan") == {
        "state": {"version": 2, "nodes": {"s1": {"status": "completed"}}},
        "last_seq": 5,
    }
    assert run.cursor("reactive") == 9
    d = run.to_dict()
    assert d["schema_version"] == 2
    assert "plan_state" not in d and d["cursors"]["plan"]["last_seq"] == 5


def test_from_dict_warns_but_loads_a_newer_schema_checkpoint(caplog):
    future = AgentRun(run_id="r", task="t").to_dict()
    future["schema_version"] = 99
    future["some_future_field"] = {"unknown": True}

    with caplog.at_level("WARNING"):
        restored = AgentRun.from_dict(future)

    assert restored.run_id == "r"
    assert "newer" in caplog.text
