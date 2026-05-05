> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# ADR-006: Trace Persistence Schema and LangSmith Alignment

**Status:** Proposed  
**Date:** 2026-05-04  
**Decision:** Treat **`TraceWriter`**-backed **local SQLite persistence** as the **authoritative source of truth** for observability. LangSmith integrates as an optional, visualization-oriented projection keyed by **`trace_id`**.

## Context

Observability is a first-class Catalyst claim (**strategy spec §§1–2**, **exec-spec §10**). P0 persisted node-level **`trace_events`** and run headers with UUID stability suitable for deterministic replay tooling. Architecture plan **v2 promotes W-06 (LangSmith) to P1-T14**. This ADR defines how external tracing layers attach **without replacing** SQLite durability or silently changing offline behavior.

## Decision

- **Source of truth:** All mandatory fields enumerated in **`full-version-execution-spec.md` §10** continue to reside in **`agent_runs` / `trace_events`** SQLite tables initialized via `catalyst_agents.trace.schema`.
- **`trace_id` mapping rule:** Exactly **one canonical `trace_id` string per attribution run**. Any LangSmith / LangChain tracer configuration MUST propagate the **same literal** (`metadata`, `tags`, or equivalent) whenever remote tracing is active so operators can **`JOIN` mentally**: `SQLite.run.trace_id == LangSmith.run.external_id/metadata`.
- **Environment variables:** P1 adopts `LANGCHAIN_TRACING_V2=true` and **`LANGCHAIN_PROJECT=catalyst`** (per architecture plan) for standard LangChain integrations; deviations require ADR amendment.
- **Graph invocation contract:** **`graph.invoke` / wrappers pass `trace_id` into runnable configuration or metadata blobs** prescribed by whichever LangGraph/LangChain stack version is pinned at implementation time — the invariant is semantic identity linkage, not a specific Python kwarg spelling.
- **Degradation semantics:** **`LANGCHAIN_TRACING_V2` unset/false ⇒ zero remote side effects.** Core graph semantics, statuses, SQLite tracing, eval numerics MUST remain bitwise-equivalent modulo ordinary nondeterministic LLM sampling already controlled elsewhere.
- **LangSmith visualization only:** Debugging dashboards must never be cited as reproducibility artifacts superseding **`catalyst_eval_frozen.db`** + local trace exports.
- **Report headers (`P1-T14` acceptance):** Markdown/JSON evaluation comparison outputs include **`langsmith_project`** reflecting the resolved project slug (typically `catalyst`) when tracing is configured, or **`null`/explicit sentinel** documenting disabled tracing — consistent with reproducibility manifests.

### Alternatives considered

- **LangSmith as authoritative store:** Rejected — violates offline/defense reproducibility mandates and introduces network coupling.
- **Duplicate trace identifiers per node in LangSmith without run-level join key:** Rejected — breaks incident response workflows (“show me this NVDA replay”).
- **Mandatory LangSmith:** Rejected — conflicts with W-06 graceful degradation stance in architecture plan commentary.

### Consequences

**Positive**

- Committee story “inspectable pipelines” ties directly to **`trace_id` joins across local + SaaS tooling.
- OSS contributors without LangSmith credentials remain unblocked.

**Negative**

- Drift risk if upstream LangChain env var schemas rename — mitigation via version pinning + smoke test.

### Follow-up tasks

- Implement `export_run`/utility enhancements if needed so LangSmith admins can reconcile costs vs SQLite token accounting.
- Add CI smoke asserting env-off mode still writes SQLite rows.

### References

- `docs/full-version-execution-spec.md` — §10 (Observability + Trace minimum fields); **§15 (ADR backlog ADR-006)**.
- `docs/full-version-design-delta.md` — §9 (observability / LangSmith bullets in Appendix B mapping).
- `docs/plans/2026-05-04-p1-p2-architecture-plan.md` — **§1.2 P1-T14** acceptance (`langsmith_project` header mandate, `LANGCHAIN_PROJECT=catalyst`).
- `docs/decisions/` — contextual pointer: W-06 promotion narrative lives in architecture plan §1.2 (no separate waiver file required here).

### Implementation notes

- **Writer lifecycle:** `packages/agents/catalyst_agents/trace/writer.py` — `TraceWriter` bootstraps `run_id`, `trace_id`, inserts `RUNNING`.
- **Schema migration hooks:** `packages/agents/catalyst_agents/trace/schema.py` (`init_trace_db`).
- Tests under `packages/agents/tests/test_trace.py` demonstrate status transitions persisted per node expectations.

### Related ADRs

- **ADR-005** — Validator status transitions traced per event.
- **ADR-009** — Reranker availability differences should be inferable alongside retrieval trace spans once P1 overlays LangSmith tooling.
- **ADR-011** — Eval exporters must propagate header fields uniformly.
- **ADR-007** (P2 stub) — Model routing overlays additional `model_id_per_role` fields in **`TraceWriter.finalize`** payload without redefining `trace_id` semantics.
