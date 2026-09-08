# Example guide: what each example demonstrates

Every example uses `ScriptedLlm`, which plays the model from a script —
**offline, zero-cost, deterministic**. Run one from the repository root:

```bash
PYTHONPATH=. python examples/file_name.py
```

Read them in number order; they form a gentle ramp. The parenthetical is what to
watch for in the code.

## Start with two "raw kernel" examples

- **graph_demo.py**: runs a graph with Plan/Node/Edge/Scheduler and no model at
  all. Watch how waves advance round by round and how state folds at the barrier.
  The shortest path to understanding the engine.
- **react_demo.py**: hand-assembles a ReAct loop from the plainest primitives.
  Watch how the think⇄act back-edge is one `Goto` — after this, ReAct is clearly
  not framework magic.

## Then the graduated business examples

- **01_greeter.py**: the smallest facade usage, one Agent + one tool. Watch how
  `Agent.run()` is still that same graph underneath.
- **02_trader.py**: multi-round bargaining, a human approval gate before a write,
  and cross-turn memory. Watch how `wait_human` really suspends the run and
  `resume` continues from the breakpoint.
- **03_deep_research.py**: consecutive retrieval rounds that trigger five-level
  context compression. Watch how context isn't memory but a projection assembled
  fresh each time.
- **04_compliance_audit.py**: several checks run in parallel; when one is rejected
  at approval, the other conclusions aren't discarded. Watch wave concurrency and
  "error is feedback, you can fix it".
- **05_code_detective.py**: wires in an MCP tool, loads a Skill from disk, and has
  the model correct itself after a tool failure. Watch how an MCP tool is
  normalized to an ordinary tool at the boundary.
- **06_after_sales.py**: the supervisor agent's "tools" are other agents:
  dispatch the billing specialist for facts, the risk specialist for a verdict,
  then decide itself. Watch each delegation go out and come back (call), and
  the three-level tree that grows when risk delegates further.
- **07_aiops.py**: the diagnose node uses call to get a result back; when it
  decides to hand over to repair it uses transfer — a same-graph `go` with no
  return edge, control leaving for good. Compare the two multi-agent convergence
  semantics.
- **08_write_review.py**: a writer agent drafts, a critic agent reviews, and a
  conditional branch carries a failing review back for revision while a passing
  one goes straight to finalize. Watch quality iteration be an ordinary loop
  with a back edge, agents sitting in the nodes.

## Finally, three "production capability" examples

- **09_persistence.py**: after checkpoints hit disk, a brand-new process resumes
  from the breakpoint. Watch what a Run snapshot stores and which live objects
  (connections, credentials) are never serialized.
- **10_retry_timeout.py**: per-node timeout and exponential-backoff retry. Watch
  "a timeout counts as one failure; whether to retry is a replaceable policy".
- **11_backpressure.py**: a node streams high-frequency events through the Bus;
  the subscriber uses a bounded queue with block/drop backpressure. Watch how
  pressure is pushed all the way back to the producer.

## See it all at once: the Playground

`make play` starts a UI where you switch scenarios on the left and see the event
timeline from the same Bus on the right — parallelism, suspended approvals,
delegation and handoff are all visible. It runs the very same kernel as these
examples; it just draws the process.
