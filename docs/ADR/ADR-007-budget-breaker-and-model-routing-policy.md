# ADR-007: Per-Run Budget and Model Routing

- Status: Accepted scope; numeric defaults remain configuration
- Owner: `catalyst-agents`, wired by `catalyst-app`

## Context

Catalyst needs bounded retries and observable cost without pretending one provider/model price table is complete. Unknown model pricing must not crash an attribution run or silently claim a known zero cost.

## Decision

- Every run may configure `max_tokens`, `max_cost_usd`, `max_latency_ms`, maximum retrieval expansion, and maximum repair calls.
- Repair calls are capped at one in the Core milestone. Retrieval expansion is disabled unless explicitly configured and traced.
- Before a model call, the runtime checks deterministic token/call limits. After a call, it records actual usage and evaluates cost/latency caps.
- Known prices produce `cost_status="known"` and a numeric `cost_usd`.
- Unknown prices produce `cost_status="unknown"` and `cost_usd=null`; they never become zero-cost claims and never crash the agent path. Evaluation configurations that require a cost comparison reject unknown pricing explicitly.
- Budget exhaustion is a named runtime condition. It yields `PARTIAL` only when a valid supported subset already exists; otherwise it yields `SYSTEM_ERROR` or `ABSTAIN` according to whether execution failed or evidence was genuinely insufficient.
- Model-provider clients and credentials live in `catalyst-app`; agents receives a `ModelClient` protocol and sanitized model identity.

## Consequences

- Cost accounting remains honest across BYOK providers.
- Model routing can change without importing provider SDKs into agents core.
- Budget state is visible in trace and `RunAssuranceRecord`.

## Implementation anchors

- `packages/agents/catalyst_agents/cost_tracker.py`
- `packages/agents/catalyst_agents/backoff.py`
- `packages/agents/catalyst_agents/runtime/`
- `packages/app/catalyst_app/llm_factory.py`
- `docs/plans/2026-07-21-b2-b7-technical-contracts.md`
