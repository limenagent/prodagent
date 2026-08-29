"""Determinism ports — the sanctioned source of time, randomness and ids.

Why ports at all: replay equivalence (the Replay laws) holds only when every
value that can differ between two executions enters through a seam we can
substitute. ``uuid4`` / ``time.time`` / ``time.monotonic`` / ``random.*``
scattered through the kernel defeat that — so they are banned in-domain
(ruff ``TID251``, report-only first, error later) and every domain call site
draws from the accessors below instead.

Why ``base`` and not ``ports``: the layering contract pins ``base`` as the
bottom layer every package may import — and every package (base included,
e.g. ``Event.make`` stamping event ids) needs these accessors. ``ports``
re-exports the protocols for API uniformity, but the module itself must sit
below everything.

The default implementations delegate to the very same stdlib calls, so
installing nothing reproduces today's behaviour bit-for-bit. Substitutes
(RecordedClock, SeededRandom, ReplayIds — the cassette runtime) install via
contextvars, which buys per-async-task isolation for free: a replay running
in one task cannot leak its frozen clock into a sibling task, and the
``override`` context manager resets on exception so a failed replay never
leaves a frozen clock behind in the surrounding context.

Wall vs monotonic stay separate methods on one port on purpose: replay
substitutes both (virtual time), while live code picks the right clock for
the job — wall for timestamps that humans compare across processes,
monotonic for durations that clock jumps must not fake.
"""

from __future__ import annotations

import random
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "IdPort",
    "RandomPort",
    "SystemIds",
    "SystemRandomness",
    "SystemTime",
    "TimePort",
    "current_ids",
    "current_random",
    "current_time",
    "new_uuid4",
    "now_monotonic",
    "now_wall",
    "override",
    "random_uniform",
]


class TimePort(Protocol):
    """The two clocks, behind one seam so replay can freeze both."""

    def wall(self) -> float:
        """Epoch seconds — for timestamps humans compare across processes."""
        ...

    def monotonic(self) -> float:
        """Arbitrary-origin seconds — for durations clock jumps must not fake."""
        ...


class RandomPort(Protocol):
    """Randomness behind a seam. One method today: retry jitter is the only
    in-domain randomness; grow the port when a second consumer appears."""

    def uniform(self, lo: float, hi: float) -> float: ...


class IdPort(Protocol):
    """Identity minting behind a seam — replay re-mints recorded ids."""

    def uuid4(self) -> str: ...


class SystemTime:
    """Default clock — the real stdlib time, unchanged."""

    def wall(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


class SystemRandomness:
    """Default randomness — the real stdlib generator."""

    def uniform(self, lo: float, hi: float) -> float:
        return random.uniform(lo, hi)


class SystemIds:
    """Default identity — real uuid4 strings."""

    def uuid4(self) -> str:
        return str(uuid.uuid4())


# B039 (mutable ContextVar default) is waived on purpose: the System* defaults
# are stateless delegators — they hold no attributes, so the cross-context
# mutation hazard the rule guards against cannot occur, while a None default
# would tax every accessor with a fallback.
_time: ContextVar[TimePort] = ContextVar("prodagent_determinism_time", default=SystemTime())  # noqa: B039
_random: ContextVar[RandomPort] = ContextVar(
    "prodagent_determinism_random",
    default=SystemRandomness(),  # noqa: B039
)
_ids: ContextVar[IdPort] = ContextVar("prodagent_determinism_ids", default=SystemIds())  # noqa: B039


def current_time() -> TimePort:
    """The installed time port (substitute it, don't call it, in new code)."""
    return _time.get()


def current_random() -> RandomPort:
    return _random.get()


def current_ids() -> IdPort:
    return _ids.get()


def now_wall() -> float:
    """Epoch seconds from the installed time port — the domain's ``time.time``."""
    return _time.get().wall()


def now_monotonic() -> float:
    """Monotonic seconds from the installed time port — the domain's
    ``time.monotonic``."""
    return _time.get().monotonic()


def new_uuid4() -> str:
    """A uuid4 string from the installed id port — the domain's ``str(uuid4())``."""
    return _ids.get().uuid4()


def random_uniform(lo: float, hi: float) -> float:
    """``random.uniform`` from the installed random port."""
    return _random.get().uniform(lo, hi)


@contextmanager
def override(
    *,
    time_port: TimePort | None = None,
    random_port: RandomPort | None = None,
    id_port: IdPort | None = None,
) -> Iterator[None]:
    """Install substitute ports for the duration of the block.

    Keyword-only so a call site reads as a labelled swap, not positional
    trivia. Tokens reset in reverse install order and even on exception —
    a crashed replay must not leave a frozen clock behind.
    """
    time_token = _time.set(time_port) if time_port is not None else None
    random_token = _random.set(random_port) if random_port is not None else None
    id_token = _ids.set(id_port) if id_port is not None else None
    try:
        yield
    finally:
        if id_token is not None:
            _ids.reset(id_token)
        if random_token is not None:
            _random.reset(random_token)
        if time_token is not None:
            _time.reset(time_token)
