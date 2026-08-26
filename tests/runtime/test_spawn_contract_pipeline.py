from __future__ import annotations

from dataclasses import replace as _dc_replace
from typing import TYPE_CHECKING

from prodagent import Agent, AgentConfig, ExecutionMode
from prodagent.backends.memory.dead_letter import InMemoryDeadLetterQueue
from prodagent.base.config import FrameworkConfig
from prodagent.coordination.messaging.contract import MessageContract
from prodagent.coordination.spawn import Spawn
from prodagent.kernel.types import LLMResponse
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.runtime.parent_runtime import ParentRuntime

if TYPE_CHECKING:
    from pathlib import Path


def _reactive_child(*, output_contract: MessageContract | None = None) -> Agent:
    return Agent(
        "responder",
        system_prompt="reply with status",
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name="responder",
            output_contract=output_contract,
            description="Returns a status string",
        ),
    )


def _isolated_fw(tmp_path: Path) -> FrameworkConfig:
    """Isolated runs_dir so the test never loads a stale checkpoint from a
    prior run — the child lazy-resolves the checkpoint store from this config."""
    fw = FrameworkConfig.default()
    return _dc_replace(
        fw,
        orchestration=_dc_replace(
            fw.orchestration,
            runs_dir=str(tmp_path / "runs"),
            events_dir=str(tmp_path / "events"),
        ),
    )


def _pipeline(
    child: Agent,
    tmp_path: Path,
    *,
    dlq: InMemoryDeadLetterQueue | None = None,
    child_output: str = "order #123 shipped",
) -> Spawn:
    return Spawn(
        [child],
        llm=FakeLLMAdapter(responses=[LLMResponse(content=child_output, stop_reason="end_turn")]),
        hooks=None,
        framework_config=_isolated_fw(tmp_path),
        ctx=ParentRuntime(parent_run_id="parent-test"),
        dead_letter_queue=dlq,
    )


async def test_contract_passes_returns_clean_result(tmp_path: Path):
    child = _reactive_child()
    pipeline = _pipeline(child, tmp_path)

    result = await pipeline.spawn("responder", "check order 123")

    assert result["state"] == "completed"
    assert "shipped" in result["output"]
    for k in ("reasoning", "thoughts", "scratchpad", "cot", "think"):
        assert k not in result


async def test_strict_contract_violation_rejects_and_notifies_dlq(tmp_path: Path):
    contract = MessageContract(
        required_fields=["output", "state"],
        field_types={"output": int, "state": str},
        strict=True,
    )

    class _SpyDLQ(InMemoryDeadLetterQueue):
        def __init__(self) -> None:
            super().__init__(max_retries=3)
            self.calls: list[tuple[str, str]] = []

        async def on_failure(self, message_id: str, payload: dict, error: str) -> str:
            self.calls.append((message_id, error))
            return await super().on_failure(message_id, payload, error)

    dlq = _SpyDLQ()
    child = _reactive_child(output_contract=contract)
    pipeline = _pipeline(child, tmp_path, dlq=dlq)

    result = await pipeline.spawn("responder", "check order 123")

    assert result["state"] == "contract_violation"
    assert "contract" in result["output"].lower()
    assert len(dlq.calls) == 1
    assert "output" in dlq.calls[0][1] or "int" in dlq.calls[0][1]


async def test_lenient_contract_violation_passes_raw_result(tmp_path: Path):
    contract = MessageContract(
        required_fields=["output"],
        field_types={"output": int},
        strict=False,
    )
    dlq = InMemoryDeadLetterQueue(max_retries=3)
    child = _reactive_child(output_contract=contract)
    pipeline = _pipeline(child, tmp_path, dlq=dlq)

    result = await pipeline.spawn("responder", "check order 123")

    assert result["state"] != "contract_violation"
    assert result["state"] == "completed"
    assert "shipped" in result["output"]
