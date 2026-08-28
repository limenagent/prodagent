"""RunRegistry — discover examples, launch them, track live runs.

Examples are discovered from the ``examples/`` tree by their READMEs (title +
「示例 #N ——」 line), loaded as modules, and driven in-process; run state
itself never lives here (checkpoints/sessions own it) — the registry only
maps run ids to the queues and cancellers the web UI needs.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import re
import sys
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar, cast, runtime_checkable

from prodagent.kernel.state import is_child_run_id
from prodagent.kernel.types import RunState

if TYPE_CHECKING:
    from prodagent.base.config import FrameworkConfig
    from prodagent.base.session import ConversationSession
    from prodagent.kernel.state import AgentRun
    from prodagent.ports import CheckpointStore, SessionStore
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)

_EXAMPLES_ROOT = Path(__file__).resolve().parents[3] / "examples"

_HITL_EXAMPLES: set[str] = {"trader", "aiops", "compliance_audit"}

_NUM_RE = re.compile(r"^>\s*示例\s*#(\d+)\s*——\s*(.+)$")
_TITLE_RE = re.compile(r"^#\s+(.+)$")


@runtime_checkable
class SupportsAclose(Protocol):
    async def aclose(self) -> None: ...


_K = TypeVar("_K")
_V = TypeVar("_V")


class _LRUCache(Generic[_K, _V]):
    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._data: OrderedDict[_K, _V] = OrderedDict()

    def get(self, key: _K) -> _V | None:
        v = self._data.get(key)
        if v is not None:
            self._data.move_to_end(key)
        return v

    def put(self, key: _K, value: _V) -> None:
        self._data[key] = value
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def values(self) -> list[_V]:
        return list(self._data.values())

    def clear(self) -> None:
        self._data.clear()


@dataclass(frozen=True, slots=True)
class ExampleSpec:
    name: str
    number: int
    title: str
    description: str
    factory: Callable[[str], Agent] | None
    default_task: str
    is_hitl: bool
    framework_config: FrameworkConfig
    multiagent_adapter: Callable[[], Any] | None = None
    """Factory for a :class:`~prodagent.playground.multiagent.MultiAgentAdapter`.

    ``None`` for single-agent-only examples. Present when the example has a
    ``multiagent.py`` module with a ``build_adapter`` callable. The frontend
    switches to the three-column multi-agent UI when this is set.
    """

    @property
    def is_multiagent(self) -> bool:
        return self.multiagent_adapter is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "number": self.number,
            "title": self.title,
            "description": self.description,
            "default_task": self.default_task,
            "is_hitl": self.is_hitl,
            "is_multiagent": self.is_multiagent,
        }


def _parse_readme(readme: Path) -> tuple[int, str, str]:
    title = ""
    number = 0
    desc = ""
    for line in readme.read_text(encoding="utf-8").splitlines():
        if not title:
            m = _TITLE_RE.match(line)
            if m:
                title = m.group(1).strip()
        if not desc:
            m = _NUM_RE.match(line)
            if m:
                number = int(m.group(1))
                desc = m.group(2).strip()
        if title and desc:
            break
    return number, title, desc


def _append_suffix(path: str, suffix: str) -> str:
    if not path:
        return path
    return f"{path}{suffix}"


def _framework_config_for(name: str) -> FrameworkConfig:
    from prodagent.base.config import FrameworkConfig, production

    # The playground is the production cockpit: durability for stateless
    # resume, spans for the event cards, the HITL gate for approval UX.
    fw = production(FrameworkConfig.from_env())
    suffix = f"-playground-{name}"
    base_pg = fw.backend.postgres_namespace
    fw.backend.postgres_namespace = f"{base_pg}{suffix}" if base_pg else f"playground-{name}"
    orch = fw.orchestration
    orch.runs_dir = _append_suffix(orch.runs_dir, suffix)
    orch.sessions_dir = _append_suffix(orch.sessions_dir, suffix)
    orch.events_dir = _append_suffix(orch.events_dir, suffix)
    return fw


def _make_factory(
    name: str,
    build_fn: Callable[..., Agent],
    fw: FrameworkConfig,
) -> Callable[[str], Agent]:
    params = inspect.signature(build_fn).parameters

    def factory(run_id: str) -> Agent:
        kwargs: dict[str, object] = {}
        if "framework_config" in params:
            kwargs["framework_config"] = fw
        elif "framework" in params:
            kwargs["framework"] = fw
        if "run_id" in params:
            kwargs["run_id"] = run_id
        return build_fn(**kwargs)

    return factory


def _load_factory(name: str, agent_py: Path) -> tuple[Callable[..., Agent], str]:
    mod_name = f"_playground_example_{name}"
    factory_attr = f"build_{name}_agent"
    if mod_name in sys.modules:
        module = sys.modules[mod_name]
        return getattr(module, factory_attr), getattr(module, "DEFAULT_TASK", "")
    pkg_root = agent_py.parent.parent
    if str(pkg_root) not in sys.path:
        sys.path.insert(0, str(pkg_root))
    spec = importlib.util.spec_from_file_location(mod_name, agent_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {agent_py}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return getattr(module, factory_attr), getattr(module, "DEFAULT_TASK", "")


def _load_multiagent_adapter(name: str, multiagent_py: Path) -> Callable[[], Any]:
    """Load ``build_adapter()`` from ``<name>/<name>/multiagent.py``.

    Mirrors :func:`_load_factory` but for the multi-agent adapter. The callable
    returns a fresh :class:`~prodagent.playground.multiagent.MultiAgentAdapter`
    instance each invocation — adapters are stateful and must not be reused
    across runs.
    """
    mod_name = f"_playground_example_multiagent_{name}"
    if mod_name in sys.modules:
        module = sys.modules[mod_name]
        return cast("Callable[[], Any]", module.build_adapter)
    pkg_root = multiagent_py.parent.parent
    if str(pkg_root) not in sys.path:
        sys.path.insert(0, str(pkg_root))
    spec = importlib.util.spec_from_file_location(mod_name, multiagent_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {multiagent_py}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return cast("Callable[[], Any]", module.build_adapter)


def discover_examples() -> list[ExampleSpec]:
    specs: list[ExampleSpec] = []
    for example_dir in sorted(_EXAMPLES_ROOT.iterdir()):
        name = example_dir.name
        readme = example_dir / "README.md"
        agent_mod_path = example_dir / name / "agent.py"
        multiagent_mod_path = example_dir / name / "multiagent.py"
        # Accept the example if it has a README and at least one of the two
        # entry modules. agent.py → single-agent factory; multiagent.py →
        # multi-agent adapter factory. An example may have both.
        if not readme.exists() or (
            not agent_mod_path.exists() and not multiagent_mod_path.exists()
        ):
            continue
        try:
            number, title, desc = _parse_readme(readme)
            fw = _framework_config_for(name)
            factory: Callable[[str], Agent] | None = None
            default_task = ""
            if agent_mod_path.exists():
                build_fn, default_task = _load_factory(name, agent_mod_path)
                factory = _make_factory(name, build_fn, fw)
            adapter_factory: Callable[[], Any] | None = None
            if multiagent_mod_path.exists():
                adapter_factory = _load_multiagent_adapter(name, multiagent_mod_path)
        except Exception:  # noqa: BLE001 — discovery must survive a bad example
            logger.exception("[playground] failed to load %s", name)
            continue
        specs.append(
            ExampleSpec(
                name=name,
                number=number,
                title=title or name,
                description=desc or "",
                factory=factory,
                default_task=default_task,
                is_hitl=name in _HITL_EXAMPLES,
                framework_config=fw,
                multiagent_adapter=adapter_factory,
            )
        )
    specs.sort(key=lambda s: s.number)
    return specs


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    example: str
    state: RunState
    pending_approval_id: str | None
    pending_handoff_peer: str | None
    final_output: str | None
    last_error: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "example": self.example,
            "state": self.state.value,
            "pending_approval_id": self.pending_approval_id,
            "pending_handoff_peer": self.pending_handoff_peer,
            "final_output": self.final_output,
            "last_error": self.last_error,
        }


@dataclass(frozen=True, slots=True)
class ReconstructResult:
    agent: Agent
    example_name: str
    run: AgentRun | None = None
    session: ConversationSession | None = None
    target_run_id: str = ""


class RunReconstructError(Exception):
    def __init__(self, reason: str, run_id: str, *, status_code: int = 404) -> None:
        self.run_id = run_id
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"{reason}: {run_id}")


CheckpointFactory = Callable[[ExampleSpec], "CheckpointStore"]
SessionStoreFactory = Callable[[ExampleSpec], "SessionStore"]


def _default_checkpoint_factory(spec: ExampleSpec) -> CheckpointStore:
    from prodagent.backends.factory import resolve_checkpoint

    return resolve_checkpoint(spec.framework_config)


def _default_session_store_factory(spec: ExampleSpec) -> SessionStore:
    from prodagent.backends.factory import resolve_session_store

    return resolve_session_store(spec.framework_config)


class RunRegistry:
    def __init__(
        self,
        specs: list[ExampleSpec],
        *,
        checkpoint_for: CheckpointFactory | None = None,
        session_store_for: SessionStoreFactory | None = None,
    ) -> None:
        self._specs: dict[str, ExampleSpec] = {s.name: s for s in specs}
        self._checkpoint_factory = checkpoint_for or _default_checkpoint_factory
        self._session_store_factory = session_store_for or _default_session_store_factory
        self._checkpoint_cache: _LRUCache[str, CheckpointStore] = _LRUCache(maxsize=64)
        self._session_cache: _LRUCache[str, SessionStore] = _LRUCache(maxsize=64)

    def checkpoint_for(self, name: str) -> CheckpointStore:
        cached = self._checkpoint_cache.get(name)
        if cached is not None:
            return cached
        spec = self._specs[name]
        store = self._checkpoint_factory(spec)
        self._checkpoint_cache.put(name, store)
        return store

    def session_store_for(self, name: str) -> SessionStore:
        cached = self._session_cache.get(name)
        if cached is not None:
            return cached
        spec = self._specs[name]
        store = self._session_store_factory(spec)
        self._session_cache.put(name, store)
        return store

    async def aclose(self) -> None:
        stores = self._checkpoint_cache.values() + self._session_cache.values()
        self._checkpoint_cache.clear()
        self._session_cache.clear()
        for store in stores:
            if not isinstance(store, SupportsAclose):
                continue
            try:
                await store.aclose()
            except Exception:  # noqa: BLE001 — shutdown must not cascade
                logger.warning("[playground] failed to close store pool", exc_info=True)
        closed: set[int] = set()
        for spec in self._specs.values():
            reg = getattr(spec.framework_config, "_backend_registry", None)
            if reg is None or id(reg) in closed:
                continue
            closed.add(id(reg))
            try:
                await reg.aclose()
            except Exception:  # noqa: BLE001 — shutdown must not cascade
                logger.warning("[playground] failed to close backend registry", exc_info=True)

    async def reconstruct(self, run_id: str) -> ReconstructResult:
        if not run_id:
            raise RunReconstructError("empty run_id", run_id, status_code=400)
        if is_child_run_id(run_id):
            raise RunReconstructError(
                "child run_id cannot be resumed directly — approve via the root run",
                run_id,
                status_code=400,
            )

        spec, session = await self._find_session(run_id)
        if spec is not None and session is not None and spec.factory is not None:
            agent = spec.factory(run_id)
            return ReconstructResult(
                agent=agent,
                example_name=spec.name,
                session=session,
                target_run_id=run_id,
            )

        spec, run = await self._find(run_id)
        if spec is None or run is None:
            raise RunReconstructError("unknown run", run_id, status_code=404)

        target_run_id = run.run_id
        if run.state is not RunState.SUSPENDED and run.pending_handoff is not None:
            peer_id = await self._resolve_suspended_peer(spec, run)
            if peer_id is not None:
                target_run_id = peer_id

        if spec.factory is None:
            raise RunReconstructError(
                f"example {spec.name!r} is multi-agent only — no single-agent factory",
                run_id,
                status_code=404,
            )
        agent = spec.factory(run_id)
        return ReconstructResult(
            agent=agent,
            example_name=spec.name,
            run=run,
            target_run_id=target_run_id,
        )

    async def load_summary(self, run_id: str) -> RunSummary | None:
        if not run_id or is_child_run_id(run_id):
            return None
        spec, run = await self._find(run_id)
        if spec is None or run is None:
            return None
        return _summary_from(spec.name, run)

    async def list_all_runs(self) -> list[RunSummary]:
        results = await asyncio.gather(
            *(self._scan_example(s) for s in self._specs.values()),
            return_exceptions=True,
        )
        summaries: list[RunSummary] = []
        for spec, result in zip(self._specs.values(), results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("[run_registry] scan %s failed: %s", spec.name, result)
                continue
            summaries.extend(result)
        return summaries

    async def _resolve_suspended_peer(self, spec: ExampleSpec, root: AgentRun) -> str | None:
        from prodagent.coordination.peer import resolve_suspended_peer_run_id

        store = self.checkpoint_for(spec.name)
        return await resolve_suspended_peer_run_id(store, root.pending_handoff)

    async def _find(self, run_id: str) -> tuple[ExampleSpec | None, AgentRun | None]:
        async def probe(spec: ExampleSpec) -> tuple[ExampleSpec, AgentRun | None]:
            store = self.checkpoint_for(spec.name)
            try:
                run = await store.load(run_id)
            except Exception as exc:
                logger.warning("[run_registry] load %s in %s failed: %s", run_id, spec.name, exc)
                return spec, None
            return spec, run

        results = await asyncio.gather(*(probe(s) for s in self._specs.values()))
        for spec, run in results:
            if run is not None:
                return spec, run
        return None, None

    async def _find_session(
        self, session_id: str
    ) -> tuple[ExampleSpec | None, ConversationSession | None]:
        async def probe(
            spec: ExampleSpec,
        ) -> tuple[ExampleSpec, ConversationSession | None]:
            store = self.session_store_for(spec.name)
            try:
                session = await store.load(session_id)
            except Exception as exc:
                logger.warning(
                    "[run_registry] load session %s in %s failed: %s",
                    session_id,
                    spec.name,
                    exc,
                )
                return spec, None
            return spec, session

        results = await asyncio.gather(*(probe(s) for s in self._specs.values()))
        for spec, session in results:
            if session is not None:
                return spec, session
        return None, None

    async def _scan_example(self, spec: ExampleSpec) -> list[RunSummary]:
        store = self.checkpoint_for(spec.name)
        try:
            run_ids = await store.list_run_ids()
        except Exception as exc:
            logger.warning("[run_registry] list_run_ids %s failed: %s", spec.name, exc)
            return []
        summaries: list[RunSummary] = []
        for rid in run_ids:
            if is_child_run_id(rid):
                continue
            try:
                run = await store.load(rid)
            except Exception as exc:
                logger.warning("[run_registry] load %s in %s failed: %s", rid, spec.name, exc)
                continue
            if run is None:
                continue
            summaries.append(_summary_from(spec.name, run))
        return summaries


def _summary_from(example: str, run: AgentRun) -> RunSummary:
    peer = run.pending_handoff.peer_name if run.pending_handoff else None
    return RunSummary(
        run_id=run.run_id,
        example=example,
        state=run.state,
        pending_approval_id=run.pending_approval_id,
        pending_handoff_peer=peer,
        final_output=run.final_output,
        last_error=run.last_error,
    )


__all__ = [
    "ExampleSpec",
    "ReconstructResult",
    "RunReconstructError",
    "RunRegistry",
    "RunSummary",
    "discover_examples",
]
