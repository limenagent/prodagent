# Example guide: what each example demonstrates

Every example uses `ScriptedLlm`, which plays the model from a script —
**offline, zero-cost, deterministic**. Run one from the repository root:

```bash
PYTHONPATH=. python examples/file_name.py
```

Read them in the order listed; they form a gentle ramp. The parenthetical is
what to watch for in the code.

## Start with two "raw kernel" examples

- **graph_demo.py**: runs a graph with Plan/Node/Edge/Scheduler and no model at
  all. Watch how waves advance round by round and how state folds at the barrier.
  The shortest path to understanding the engine.
- **react_demo.py**: hand-assembles a ReAct loop from the plainest primitives.
  Watch how the think⇄act back-edge is one `Goto` — after this, ReAct is clearly
  not framework magic.

## Then the graduated business examples

- **greeter.py**: the smallest facade usage, one Agent + one tool. Watch how
  `Agent.run()` is still that same graph underneath.
- **trader.py**: multi-round bargaining, a human approval gate before a write,
  and cross-turn memory. Watch how `wait_human` really suspends the run and
  `resume` continues from the breakpoint.
- **deep_research.py**: consecutive retrieval rounds that trigger five-level
  context compression. Watch how context isn't memory but a projection assembled
  fresh each time.
- **compliance_audit.py**: several checks run in parallel; when one is rejected
  at approval, the other conclusions aren't discarded. Watch wave concurrency and
  "error is feedback, you can fix it".
- **code_detective.py**: wires in an MCP tool, loads a Skill from disk, and has
  the model correct itself after a tool failure. Watch how an MCP tool is
  normalized to an ordinary tool at the boundary.
- **after_sales.py**: the supervisor agent's "tools" are other agents:
  dispatch the billing specialist for facts, the risk specialist for a verdict,
  then decide itself. Watch each delegation go out and come back (call), and
  the three-level tree that grows when risk delegates further.
- **aiops.py**: the diagnose node uses call to get a result back; when it
  decides to hand over to repair it uses transfer — a same-graph `go` with no
  return edge, control leaving for good. Compare the two multi-agent convergence
  semantics.
- **handoff.py**: pure *transfer* with no supervisor in the flow, in two
  shapes — a fixed handoff **chain** (triage → billing → risk → close) and a
  **swarm net** where peers can hand the case back (billing ↔ risk) before
  settling. The graph registers the Agent nodes but draws no edges between
  them: the next holder is a runtime `go` named in the specialist's own verdict.
  Watch control leave for good while only a one-line summary travels forward.
- **write_review.py**: a writer agent drafts, a critic agent reviews, and a
  conditional branch carries a failing review back for revision while a passing
  one goes straight to finalize. Watch quality iteration be an ordinary loop
  with a back edge, agents sitting in the nodes.
- **orchestrator.py**: orchestrator-worker. The graph has ONE reviewer node:
  a planner agent calls a live catalog tool, writes a numbered plan, and each
  line becomes a `Send` that stamps out one template copy — all copies run in
  one wave, a `join="all"` synth waits for them all. Watch the fan-out width be
  runtime data, not graph shape.
- **blackboard.py**: multi-round consensus. Experts never call each other —
  each only appends its opinion to one shared channel; a `join="all"` moderator
  reads the whole board and, short of consensus, `Goto.rejoin` re-arms everyone
  for another round. Watch opinions accumulate, nothing overwritten.
- **dating_chat.py** (the capstone verification scenario): an agent blind
  date — two context strategies in one shared conversation. Daniu hand-rolls
  his messages list, forwards a raw tool dump, and runs `del messages[:-4]`
  once past the threshold; Xiaomei runs on the framework: seeded long-term
  memory (recalled and injected before think) plus five-level compaction over
  a six-message budget. Her review check catches the trap — the compressed
  result keeps the seafood evidence at the head and the noise level at the
  tail — and the closing verdict shows who still remembered: her summary
  carries the allergy line, his truncated window lost it.

## Finally, three "production capability" examples

- **persistence.py**: after checkpoints hit disk, a brand-new process resumes
  from the breakpoint. Watch what a Run snapshot stores and which live objects
  (connections, credentials) are never serialized.
- **retry_timeout.py**: per-node timeout and exponential-backoff retry. Watch
  "a timeout counts as one failure; whether to retry is a replaceable policy".
- **backpressure.py**: a node streams high-frequency events through the Bus;
  the subscriber uses a bounded queue with block/drop backpressure. Watch how
  pressure is pushed all the way back to the producer.

## See it all at once: the Playground

`make play` starts a UI where you switch scenarios on the left (the business
scenarios 01-10 in browser form, plus a cross-session-memory bonus; run the
mechanism demos 11-13 in a terminal) and see the event timeline from the same
Bus on the right — parallelism, suspended approvals, delegation and handoff are
all visible. It runs the very same kernel as these examples; it just draws the
process.
