# ADR-005: Output Status and Downgrade Policy

- Status: Accepted, amended for the Core milestone
- Owner: `catalyst-agents`

## Context

Catalyst must distinguish insufficient evidence, degraded but usable output, and operational failure. Treating malformed model output as financial uncertainty creates a misleading report; treating missing evidence as a system crash makes abstention unreliable.

## Decision

The public attribution statuses are:

- `SUFFICIENT`: at least one gate-passed hypothesis is supported, citations resolve, the Judge saw every cited item, no cutoff violation exists, and required context is complete;
- `PARTIAL`: a supported explanation exists, but coverage is partial/degraded or support is limited to opinion, unknown-origin aggregation, or uncorroborated issuer claims;
- `ABSTAIN`: target/context quality is inadequate, zero usable evidence exists, or all hypothesis gates fail;
- `SYSTEM_ERROR`: model, schema, database, provider, timeout, or runtime failure prevents a valid decision.

Validator may make at most one structured repair call. If repair succeeds, Finalizer evaluates the repaired output normally. If repair fails and no valid supported output remains, status is `SYSTEM_ERROR`; malformed output is never presented as evidence insufficiency. If a previously valid supported subset remains and only optional fields cannot be repaired, Finalizer may emit `PARTIAL` with an explicit validation-degraded reason.

Finalizer respects a valid terminal status already produced by an earlier deterministic handler. It never turns `SYSTEM_ERROR` into `ABSTAIN`, or `ABSTAIN` into `PARTIAL`, merely because a fallback sentence exists.

## Consequences

- The workbench can say “insufficient evidence” without hiding operational failures.
- PARTIAL requires a usable supported subset; it is not a catch-all error bucket.
- Trace and assurance records persist the status reason and repair count.

## Implementation anchors

- `packages/agents/catalyst_agents/nodes/validator.py`
- `packages/agents/catalyst_agents/nodes/finalizer.py`
- `packages/agents/catalyst_agents/state.py`
- `packages/agents/tests/test_validator.py`
- `packages/agents/tests/test_failure_taxonomy.py`
- `docs/plans/2026-07-21-b2-b7-technical-contracts.md`
