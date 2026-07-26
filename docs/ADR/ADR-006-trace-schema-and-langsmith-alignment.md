# ADR-006: Local Trace Schema and Optional External Tracing

- Status: Accepted, amended for the Core milestone
- Owner: `catalyst-agents`

## Context

Replay, runtime assurance, and contributor debugging require durable local traces. Optional external tracing may help development, but cannot become a correctness dependency or reproducibility artifact.

## Decision

- SQLite `agent_runs`, `trace_events`, `node_artifacts`, and `run_links` are the source of truth.
- One attribution run has one stable `run_id` and one stable `trace_id`.
- Required trace data includes node sequence, timestamps, status transition, model identity, token/cost usage, decision metadata, errors, corpus/index identity, prompt identity, cutoff, and artifact references.
- Trace schema gains an explicit version before B5 assurance records are added. Readers fail loudly on unsupported versions.
- External tracing is optional and disabled by default. When enabled, it receives the same run/trace identities but never replaces local persistence.
- Environment-off mode has zero remote side effects and identical local workflow behavior.
- Secrets and full provider payloads never enter trace metadata.

## Consequences

- Zero-network replay and assurance remain available on a laptop.
- Contributors can inspect a run without a hosted account.
- Optional tracing adapters can be added without changing the core artifact contract.

## Implementation anchors

- `packages/agents/catalyst_agents/trace/schema.py`
- `packages/agents/catalyst_agents/trace/writer.py`
- `packages/agents/catalyst_agents/trace/artifacts.py`
- `packages/agents/tests/test_trace.py`
- `docs/plans/2026-07-19-catalyst-final-package-architecture.md`
- `docs/plans/2026-07-21-b2-b7-technical-contracts.md`
