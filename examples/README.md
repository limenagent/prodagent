# Examples

Every example runs **offline and deterministically**: the model is played by
`ScriptedLlm` from a script, so no API key and no spend. Run any of them from
the repository root:

```bash
PYTHONPATH=. python examples/01_greeter.py
```

The numbering is the suggested reading order; `graph_demo.py` and
`react_demo.py` are the two raw-kernel demos that precede the numbered series.
The same scenarios also run in the browser via `make play` (see the
[playground](../README.md#playground-one-command-every-example-in-the-browser)).

| File | What it demonstrates |
|---|---|
| `graph_demo.py` | The raw kernel with no model at all: a diamond graph where the two middle nodes run concurrently in one wave — watch the Bus events to see how BSP supersteps advance. |
| `react_demo.py` | Hand-assembling a ReAct loop from kernel primitives: two nodes, a conditional edge, and one back edge. The kernel itself contains no ReAct pattern. |
| `01_greeter.py` | The smallest agent: one model + one tool — think once, call the tool, answer. |
| `02_trader.py` | Multi-round haggling with a read tool, a write tool behind an approval gate (first rejection feeds back into the model), and long-term memory injecting user preferences. |
| `03_deep_research.py` | Many search rounds without blowing the context window: five-level compaction as a replaceable strategy — mechanical shortening first, model-called summaries only at the summary levels. |
| `04_compliance_audit.py` | Two audit branches run in parallel and converge; the "freeze accounts" write really suspends for human approval (`wait_human`), and a denial only changes the action — no re-run of the checks. |
| `05_code_detective.py` | MCP tools normalized to ordinary tools at the boundary, and a debugging skill loaded from a `SKILL.md` on disk — fix, rerun, fix again until the tests go green. |
| `06_after_sales.py` | The supervisor pattern: a supervisor agent whose "tools" are other agents — dispatch a specialist, its answer comes back, dispatch the next, then decide. The risk specialist delegates further, growing a three-level delegation tree where every level runs the same loop. |
| `07_aiops.py` | Both multi-agent semantics in one workflow: parallel diagnosis via delegation (*call*), then a no-return-edge `go` hands off to the repair agent (*transfer*). |
| `08_write_review.py` | Generator–critic: a writer drafts, a critic reviews, and a conditional branch carries a failing review back for revision — quality iteration as an ordinary loop with agents in the nodes. |
| `09_persistence.py` | Checkpoints and the event log on disk: a **freshly built** Workflow (simulating a process restart) resumes from the exact suspension point. |
| `10_retry_timeout.py` | Per-node resilience: a `RetryPolicy` with exponential backoff plus a timeout — mechanism in the kernel, policy fully replaceable. |
| `11_backpressure.py` | Streaming from inside a node via `ctx.emit`; a bounded subscription with `block` vs `drop` overflow policies — the demo drops 6 of 8 frames, counted, while the main flow never stalls. |
