"""Curated presets — one line per common agent shape.

Each recipe mirrors a full example (compliance_audit, deep_research,
aiops/trip_planner) boiled down to its configuration skeleton. Behavioural
defaults stay with the framework (hard budget, HITL gate on HIGH side
effects, checkpoint + event log); a recipe only pins mode, sensible
limits, and the shape of the collaboration topology.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prodagent import Agent, AgentConfig, ExecutionMode, HardBudget

if TYPE_CHECKING:
    from collections.abc import Sequence

    from prodagent.guardrail.approval import ApprovalProvider
    from prodagent.ports import Tool


def audit_agent(
    name: str,
    system_prompt: str,
    tools: Sequence[Tool],
    *,
    budget: HardBudget | None = None,
    constraints: Sequence[str] = (),
    approval: ApprovalProvider | None = None,
) -> Agent:
    """Approval-gated auditor — PLAN_FIRST dynamic DAG + HITL + idempotent writes.

    Mirrors ``examples/compliance_audit``: the plan is inspectable and
    approvable before execution, HIGH side-effect tools hit the approval
    gate, and a failed step triggers incremental replan instead of a
    restart. Pass ``approval=`` to wire a real HITL provider; without one
    the default approval bundle still gates HIGH tools.
    """
    return Agent(
        name,
        system_prompt=system_prompt,
        tools=tools,
        mode=ExecutionMode.PLAN_FIRST,
        budget=budget or HardBudget(max_turns=30, max_seconds=3600.0),
        config=AgentConfig(
            name=name,
            constraints=list(constraints),
            approval=approval,
        ),
    )


def research_agent(
    name: str,
    system_prompt: str,
    tools: Sequence[Tool],
    *,
    budget: HardBudget | None = None,
    constraints: Sequence[str] = (),
) -> Agent:
    """Exploratory researcher — REACTIVE loop + five-level compression.

    Mirrors ``examples/deep_research``: no upfront plan (exploration means
    choosing the next step from the previous result), five-level context
    compression keeps long sessions inside budget, and the REACTIVE-mode
    WARNING applies — use for read-heavy, low-risk tasks.
    """
    return Agent(
        name,
        system_prompt=system_prompt,
        tools=tools,
        mode=ExecutionMode.REACTIVE,
        budget=budget or HardBudget(max_turns=40),
        config=AgentConfig(name=name, constraints=list(constraints)),
    )


def delegation_agent(
    name: str,
    system_prompt: str,
    *,
    tools: Sequence[Tool] = (),
    agents: Sequence[Agent] = (),
    peers: Sequence[Agent] = (),
    budget: HardBudget | None = None,
) -> Agent:
    """Collaboration hub — vertical delegation (``agents=``) or relay (``peers=``).

    Mirrors ``examples/aiops`` / ``trip_planner``: the parent spawns
    children that report back (results cross the messaging plane's
    admission pipeline), or hands control along a peer chain. Child spend
    rolls up into this agent's budget — the tree-shaped ledger.
    """
    if not agents and not peers:
        raise ValueError("delegation_agent needs agents= (delegate) or peers= (relay)")
    return Agent(
        name,
        system_prompt=system_prompt,
        tools=tools,
        budget=budget or HardBudget(max_turns=50),
        config=AgentConfig(name=name, agents=list(agents), peers=list(peers)),
    )
