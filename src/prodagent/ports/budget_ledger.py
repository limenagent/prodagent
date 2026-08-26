"""BudgetLedgerPort — the shared-settlement contract.

The four-axis ceiling vocabulary every coordinated spender speaks:
``reserve`` before a unit of work, ``commit`` the actuals after, ``release``
a reservation that never became real spend, ``check`` without debiting.
Members are attributed (``member=``) so one member can never release
another's spoken-for share.

The kernel's in-process implementation is
:class:`prodagent.kernel.budget.BudgetLedger` (an ``asyncio.Lock`` over
shared-by-reference counters) — it satisfies this Protocol structurally,
without inheriting anything. A distributed runtime swaps in a networked
implementation (same vocabulary, remote arbiter) without touching the
coordination primitives or the kernel envelope; the member-tagged
reserve/commit/release design is deliberately wire-friendly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.kernel.budget import HardBudget

__all__ = ["SpendView", "BudgetLedgerPort"]


@runtime_checkable
class SpendView(Protocol):
    """Read-only snapshot of spend on three axes (``seconds`` is wall-clock,
    never reserved/committed). Structural over the kernel's internal spend
    counters — implementations return fresh copies, never a live reference."""

    turns: int
    tokens: int
    cost_usd: float


@runtime_checkable
class BudgetLedgerPort(Protocol):
    """Four-axis ceiling shared across concurrent spenders.

    Construct once per coordinated run (ensemble floor, peer chain, a batch
    of concurrent spawns); every spender — local or remote — settles against
    the same logical ledger through this vocabulary.
    """

    @property
    def max(self) -> HardBudget: ...

    @property
    def committed(self) -> SpendView:
        """Settled spend only. Executors check against this view: an
        in-flight reservation gates siblings at reserve time and must not
        block the very member it was reserved for."""
        ...

    @property
    def spent(self) -> SpendView:
        """Committed + reserved (best-effort, lock-free)."""
        ...

    def member_reserved(self, member: str) -> SpendView:
        """One member's outstanding reservation."""
        ...

    def elapsed_seconds(self) -> float:
        """Wall-clock since the ledger was created — the ``seconds`` axis."""
        ...

    def is_exhausted(self) -> bool:
        """Cheap lock-free pre-check, pollable between rounds/hops. The
        authoritative check (raises with axis detail) is :meth:`check`."""
        ...

    async def check(self, *, member: str) -> None:
        """Raise ``BudgetExceeded`` if committed+reserved is at/over cap.
        Called before a unit of work starts; does not debit."""
        ...

    async def reserve(
        self,
        *,
        member: str,
        turns: int = 1,
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Pre-debit an estimate so concurrent siblings see this work as
        spoken for. Raises (without debiting) if at/over cap. Reconciled away
        by :meth:`commit` or undone by :meth:`release`."""
        ...

    async def release(
        self,
        *,
        member: str,
        reserved_turns: int = 0,
        reserved_tokens: int = 0,
        reserved_cost_usd: float = 0.0,
    ) -> None:
        """Give back a reservation that never became real spend. Never touches
        committed; can only reduce the named member's *own* reservation."""
        ...

    async def commit(
        self,
        *,
        member: str,
        turns: int,
        tokens: int,
        cost_usd: float,
        reserved_turns: int = 0,
        reserved_tokens: int = 0,
        reserved_cost_usd: float = 0.0,
    ) -> None:
        """Reconcile a reservation (if any) and commit the actual deltas."""
        ...
