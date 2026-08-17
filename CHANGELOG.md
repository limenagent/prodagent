# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Unified messaging plane** (`runtime/coordination/messaging/`) — every agent-boundary
  crossing in all five collaboration primitives now flows through one checkpoint:
  - `Crossing` envelope: direction (`DOWNSTREAM` assembly / `UPSTREAM` admission) ×
    kind (dispatch / handoff / result / speech / write / enqueue / task_result) × a
    typed payload (never flattened).
  - `Pipeline` with a fixed slot order (`DEDUPE → BEFORE_CONTRACT → CONTRACT →
    AFTER_CONTRACT → GATE → AUDIT`), a dead-letter error boundary, dedupe
    short-circuit, and lenient-contract pass-through. Two presets:
    `assembly_pipeline` (downstream) and `admission_pipeline` (upstream); the two
    open slots are user injection points for semantic policy (injection rules,
    LLM judges) — the framework ships mechanics, apps ship policy.
  - `MessageContract` (generalizes `HandoffContract`; `whitelist()` absorbs the old
    `HandoffInterceptor` filter) + built-in interceptors reusing
    `IdempotentMessageHandler`, `FloorProjection`, and the existing
    `CheckPoint.AGENT_HANDOFF` security gate (no-op unless checkers are registered).
- Migrated all five primitives onto the plane:
  - `spawn`: downstream dispatch gated before the child burns budget; the child
    result is admitted through its contract and **returned sanitized** (whitelisted
    view + accounting scalars — `tool_history` and friends no longer reach the parent).
  - `peers=`: `PendingHandoff` carries a `message_id` (minted at the handoff tool);
    the relay goes through an assembly pipeline (replay suppression + security gate);
    the chain root's `output_contract` is enforced at settle.
  - `Ensemble`: member speech is admitted before entering the shared floor — a
    poisoned turn is dead-lettered and recorded as a pass turn (the floor survives);
    `EnsembleSpec` gains `hooks` / `dead_letter` / `admission_interceptors`;
    `EnsembleCompletedEvent.final_transcript` is now a projected digest.
  - `Blackboard`: per-key value contracts (`BlackboardSpec.contracts`), bounded value
    admission and bounded board rendering; **a `VersionConflict` now isolates the
    losing expert's write (dead-lettered) instead of killing the whole board**.
  - `WorkQueue`: `payload_contract` validates item payloads at construction
    (fail-fast at the source — the durable log only ever records admitted payloads);
    worker results are admitted and a governance rejection routes through the
    existing `fail()` → retry/dead-letter path.

### Changed

- `runtime/coordination/handoff.py` dissolved: `HandoffPacket` →
  `messaging/packet.py`, `HandoffContract` → `MessageContract`, `HandoffInterceptor`
  deleted (absorbed into `ContractInterceptor` + `whitelist()`). `idempotency.py`
  moved into `messaging/`. No compatibility aliases.
- Orchestration config keys generalized beyond spawn: `spawn_idempotency_ttl_s` →
  `handoff_idempotency_ttl_s`, `spawn_handoff_output_max_chars` →
  `handoff_output_max_chars`, `spawn_dlq_max_retries` → `dead_letter_max_retries`.
- `Spawn.spawn` returns the contract-whitelisted result view plus
  `turns`/`cost_usd`/`input_tokens`/`output_tokens`; `tool_history`,
  `approval_request_id`, and `failed_reason` no longer cross the boundary.

## [1.0.0] - 2026-08-03

Initial public release — the production-hardening layer for LLM agents, and the
companion codebase for the GeekTime column _Production-Grade Agent Pitfall Field Guide_.

### Added

- **Agent runtime**
  - Three execution modes: `PLAN_FIRST` (LLM-generated, auditable DAG), `REACTIVE` (ReAct loop), `Workflow` (hand-written DAG).
  - Inter-agent collaboration: `.agents()` vertical delegation, `.peers()` horizontal handoff.
  - Four-axis hard budget (turns / seconds / tokens / cost_usd) with sub-agent cost rollup.
  - Crash recovery via checkpoint + event log, optimistic versioning, and resume-from-breakpoint.
  - Context sandwich (state / memory / skills / history / reminder) with five-level compression.
  - `@tool` declarative tool system with side-effect tiers; native MCP support.

- **Production hardening**
  - Retry with fixed / exponential / jittered backoff, classified by error code.
  - Tool-level and agent-level circuit breakers.
  - Five-layer injection-defense pipeline, three-level taint tracking, tiered tool permissions, HITL approval gates.
  - Span tracing with OTLP export and trajectory drift detection.
  - Eval harness: golden eval suite, LLM judge, CI regression.

- **Memory & evolution**
  - Four-channel long-term memory (rule / entity / exact / semantic) with ACT-R activation decay.
  - Tri-protocol hook bus (Event / CheckPoint / Injection).
  - Self-evolving skills distilled from successful runs.

- **Backends**
  - 15 protocol ports; zero-dependency `file` + `memory` default.
  - Production swaps: Postgres, Redis, Neo4j, Qdrant, OTLP.

- **Playground**
  - Web playground (`make playground`) plus a prod-backed mode (`make playground-prod`) with 8 end-to-end examples.

[1.0.0]: https://github.com/limenagent/prodagent/releases/tag/v1.0.0
