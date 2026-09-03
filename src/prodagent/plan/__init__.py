"""prodagent.plan — the LLM-bound front-ends above the kernel.

The engine (Scheduler, bootstrap, node runner, event log, finalize), the
topology (Graph/Plan/Edge), and the vocabulary (Unit/Outcome) all live in
``kernel`` now. What stays here is what genuinely needs the outside
world: the LLM planner (``planner``), the hand-written Workflow builder
(``workflow``), and the compile front-end (``ir``). The kernel drives
them through ports, never imports them.
"""
