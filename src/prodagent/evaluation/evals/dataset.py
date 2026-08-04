"""Golden dataset management with versioning for agent evaluation."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class GoldenExample:
    """A single ground-truth test case for agent evaluation."""

    id: str
    task: str
    expected_tool_sequence: list[str] | None = None
    expected_output_contains: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    max_turns: int | None = None
    constraints: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GoldenExample:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ExampleResult:
    """Result of running one GoldenExample through the agent."""

    example_id: str
    passed: bool
    turn_count: int
    cost_usd: float
    wall_seconds: float
    tool_sequence: list[str]  # actual tool calls made
    final_output: str
    failure_reason: str | None = None
    judge_score: float | None = None  # 0–1 from LLM judge (optional)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalReport:
    """Aggregated results of running a full GoldenDataset."""

    dataset_name: str
    dataset_version: str  # content hash of the GoldenDataset
    tag: str  # e.g. "baseline", "pr-1234", "v2.0"
    created_at: float
    results: list[ExampleResult] = field(default_factory=list)
    model: str = ""
    notes: str = ""

    @property
    def pass_rate(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.passed) / len(self.results)

    @property
    def mean_turns(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.turn_count for r in self.results) / len(self.results)

    @property
    def mean_cost_usd(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.cost_usd for r in self.results) / len(self.results)

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.results)

    @property
    def mean_judge_score(self) -> float | None:
        scored = [r.judge_score for r in self.results if r.judge_score is not None]
        return sum(scored) / len(scored) if scored else None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EvalReport:
        results = [ExampleResult(**r) for r in d.pop("results", [])]
        report = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        report.results = results
        return report


class GoldenDataset:
    """Versioned collection of GoldenExamples."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._examples: dict[str, GoldenExample] = {}
        self._version_cache: str | None = None

    def add(self, example: GoldenExample) -> None:
        """Add or replace an example by id."""
        self._examples[example.id] = example
        self._version_cache = None

    def all(self, *, tags: list[str] | None = None) -> list[GoldenExample]:
        """Return all examples, optionally filtered by tag intersection."""
        examples = list(self._examples.values())
        if tags:
            tag_set = set(tags)
            examples = [e for e in examples if tag_set.intersection(e.tags)]
        return examples

    def get(self, example_id: str) -> GoldenExample | None:
        return self._examples.get(example_id)

    def __len__(self) -> int:
        return len(self._examples)

    @property
    def version(self) -> str:
        """Stable content hash — changes whenever any example changes."""
        if self._version_cache is None:
            payload = json.dumps(
                [e.to_dict() for e in sorted(self._examples.values(), key=lambda e: e.id)],
                sort_keys=True,
            )
            self._version_cache = hashlib.sha256(payload.encode()).hexdigest()
        return self._version_cache

    def save(self, path: str | Path) -> Path:
        """Serialise to JSON. Returns the written path."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "name": self.name,
            "version": self.version,
            "saved_at": time.time(),
            "examples": [e.to_dict() for e in self._examples.values()],
        }
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        logger.info(
            "GoldenDataset %r saved: %d examples, version=%s",
            self.name,
            len(self._examples),
            self.version[:12],
        )
        return p

    @classmethod
    def load(cls, path: str | Path) -> GoldenDataset:
        """Load a previously saved GoldenDataset."""
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        ds = cls(name=data["name"])
        for ex_dict in data.get("examples", []):
            ds.add(GoldenExample.from_dict(ex_dict))
        logger.info(
            "GoldenDataset %r loaded: %d examples from %s",
            ds.name,
            len(ds._examples),
            p,
        )
        return ds
