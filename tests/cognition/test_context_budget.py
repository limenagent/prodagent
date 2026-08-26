import pytest

from prodagent.base.config import ContextConfig
from prodagent.cognition.context.budget import ContextBudget, Layer


@pytest.fixture
def cfg():
    return ContextConfig(max_tokens=100_000)


@pytest.fixture
def budget(cfg):
    return ContextBudget(cfg, max_tokens=100_000)


class TestBudgetAccounting:
    def test_alloc_debits_layer(self, budget):
        budget.alloc(Layer.L0, 800)
        budget.alloc(Layer.L1, 200)
        assert budget.spent() == 1000

    def test_alloc_rejects_unknown_layer(self, budget):
        with pytest.raises(ValueError):
            Layer("L4")

    def test_remaining_decrements_on_alloc(self, budget):
        assert budget.remaining() == 100_000
        budget.alloc(Layer.L0, 800)
        assert budget.remaining() == 99_200

    def test_spent_excludes_reminder_and_margin(self, budget):
        budget.alloc(Layer.L0, 800)
        budget.alloc(Layer.L1, 200)
        budget.alloc(Layer.L2, 3500)
        budget.alloc(Layer.L3, 42000)
        assert budget.spent() == 800 + 200 + 3500 + 42000

    def test_breakdown_keys_are_L0_L1_L2_L3_free(self, budget):
        budget.alloc(Layer.L0, 800)
        budget.alloc(Layer.L3, 42000)
        bd = budget.breakdown()
        assert set(bd.keys()) == {"L0", "L1", "L2", "L3", "free"}
        assert bd["L0"] == 800
        assert bd["L1"] == 0
        assert bd["free"] == 100_000 - 800 - 42000


class TestLayerBudgetEnforcement:
    def test_layer_budget_is_max_times_ratio(self, budget):
        assert budget.layer_budget(Layer.L2) == 35_000
        assert budget.layer_budget(Layer.L0) == 8_000
        assert budget.layer_budget(Layer.L3) == 42_000

    def test_layer_spent_returns_allocated(self, budget):
        budget.alloc(Layer.L2, 3500)
        assert budget.layer_spent(Layer.L2) == 3500
        assert budget.layer_spent(Layer.L1) == 0

    def test_layer_spent_rejects_unknown_layer(self, budget):
        with pytest.raises(KeyError):
            budget.layer_spent("L4")

    def test_layer_budget_rejects_unknown_layer(self, budget):
        with pytest.raises(KeyError):
            budget.layer_budget("L4")

    def test_is_over_true_when_spent_exceeds_budget(self, budget):
        budget.alloc(Layer.L2, 40_000)
        assert budget.is_over(Layer.L2) is True

    def test_is_over_false_when_within_budget(self, budget):
        budget.alloc(Layer.L2, 30_000)
        assert budget.is_over(Layer.L2) is False

    def test_is_over_false_at_exact_budget(self, budget):
        budget.alloc(Layer.L2, 35_000)
        assert budget.is_over(Layer.L2) is False


class TestPruneLayer:
    def test_prune_returns_empty_for_empty_input(self):
        from prodagent.cognition.context.budget import TokenCounter
        from prodagent.cognition.context.manager import ContextManager

        result = ContextManager._prune_layer([], 1000, TokenCounter())
        assert result == []

    def test_prune_keeps_all_when_under_budget(self):
        from prodagent.cognition.context.budget import TokenCounter
        from prodagent.cognition.context.manager import ContextManager

        counter = TokenCounter()
        items = ["short snippet one", "short snippet two"]
        result = ContextManager._prune_layer(items, 10_000, counter)
        assert result == items

    def test_prune_drops_oldest_when_over_budget(self):
        from prodagent.cognition.context.budget import TokenCounter
        from prodagent.cognition.context.manager import ContextManager

        counter = TokenCounter()
        items = ["old " * 100, "middle " * 100, "newest " * 100]
        budget = counter.count("newest " * 100) + counter.count("\n")
        result = ContextManager._prune_layer(items, budget, counter)
        assert result == ["newest " * 100]

    def test_prune_preserves_newest_order_in_output(self):
        from prodagent.cognition.context.budget import TokenCounter
        from prodagent.cognition.context.manager import ContextManager

        counter = TokenCounter()
        items = ["first", "second", "third"]
        result = ContextManager._prune_layer(items, 10_000, counter)
        assert result == ["first", "second", "third"]


class TestContextManagerL2Pruning:
    def _make_run(self, messages=None, task="test task"):
        from unittest.mock import AsyncMock

        run = AsyncMock()
        run.task = task
        run.turn_count = 1
        run.tool_failures = 0
        run.last_action = None
        run.state.value = "running"
        run.messages = messages or [{"role": "user", "content": "hello"}]
        return run

    @pytest.mark.asyncio
    async def test_l2_over_budget_prunes_memory_snippets(self):
        from prodagent.base.config import ContextConfig
        from prodagent.cognition.context.manager import ContextManager

        cfg = ContextConfig(max_tokens=10_000)
        cm = ContextManager(config=cfg, system_prompt="sys", llm=None)

        big_snippets = [f"memory fact number {i} " * 20 for i in range(50)]

        run = self._make_run(messages=[{"role": "user", "content": "hi"}])
        _, messages = await cm.prepare(run, memory_snippets=big_snippets)

        memory_msgs = [m for m in messages if str(m.get("content", "")).startswith("[MEMORY]")]
        if memory_msgs:
            from prodagent.cognition.context.budget import TokenCounter

            memory_tokens = TokenCounter().count(memory_msgs[0]["content"])
            assert memory_tokens <= 3_500
