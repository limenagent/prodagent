# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
