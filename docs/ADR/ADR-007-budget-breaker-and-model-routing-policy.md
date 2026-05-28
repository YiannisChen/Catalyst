> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# ADR-007: Budget Circuit Breaker and Model Routing Policy

**Status:** Deferred to P2 *(stub — pending detailed thresholds)*  
**Date:** 2026-05-04  
**Decision:** **Defer** full numerical policy drafting until **Phase P2** gated tasks **P2-T03 / P2-T04**. This stub records scope boundaries **without overlapping ADR-006** (tracing is orthogonal).

## Context

`docs/full-version-execution-spec.md` **§11** mandates per-run **`cost_usd`**, **`tokens`**, **`latency_ms`** ceilings plus **budget overflow ⇒ `PARTIAL` with explicit flagging** semantics. Companion requirement: **cheap model routing for Critic vs stronger synthesis model for Judge** (same §11 bullets). Architecture plan maps these to **`P2-T03`/`P2-T04`** gated by **ADR-007**.

ADR-007 intentionally **does not** redefine **`trace_id` linkage, SQLite primacy, or LangSmith envelopes** (**ADR-006** owns observability layering). Where budgets trip, traces may annotate overflow reasons, but persistence mechanics stay identical.

## Outline of future decision content (placeholder bullets)

Future **Accepted** revision must resolve:

- Default **`max_cost_usd`**, **`max_tokens`**, **`max_latency_ms`** baselines calibrated against **`mcj_full`** distributions on **`v1_2_p1_set.jsonl`** frozen runs.
- **DecisionRouter downgrade path** aligning with **`full-version-execution-spec.md` §6** (**`PARTIAL` + budget metadata** distinct from **`validator`** partialization in **ADR-005** — disambiguated via **`retrieval_metadata` / supplementary flags).
- Model routing defaults (**Critic** vs **Judge** ID selection) respecting pinned provider manifests for Tier-A reproducibility.
- Telemetry expectations: **`model_id`** per node already flows through trace writer — budgets add **budget snapshot fields** *(spec detail TBD)*.
- Harness reporting: unify **cost + latency + quality** table columns (**exec-spec §12** synergy).

### Alternatives considered

- Draft numeric thresholds prematurely in P1 documentation: **Deferred** — risks stale numbers absent vector-era cost curves.
- Merge with ADR-006: **Rejected** — conflates cost governance with telemetry transport semantics.

### Consequences

**Positive**

- Cursor / reviewers know **ADR-007 slot is intentionally empty of numbers** until P2 calibration evidence exists.

**Negative**

- Risk of teams assuming budgets already enforced — mitigation: **`cost_tracker.py` exists but lacks hard caps until P2** per plan commentary.

### Follow-up tasks

- Promote stub → full ADR ahead of **`P2-T03`** kickoff once **`packages/agents`** budget instrumentation PRD lands.
- Cross-link evaluation outputs validating Partial-class separation.

### References

- `docs/full-version-execution-spec.md` — **§§6–7 routing**, **§11 cost governance**, **§15 ADR backlog (ADR-007)**.
- `docs/plans/2026-05-04-p1-p2-architecture-plan.md` — **§2.2 `P2-T03`/`P2-T04`**, **§2.4 ADR backlog**, **risk register**.
- Related governance context: **`docs/full-version-design-delta.md`** (experiments emphasize cost disclosures).

### Implementation notes *(anticipated — not binding until coding)*

- **`packages/agents/catalyst_agents/cost_tracker.py`** — extension anchor for breaker logic.
- **Router / graph nodes** enforcing stop-expansion semantics when caps trip.

### Related ADRs

- **ADR-005** — Distinguishes validation PARTIAL vs future **budget-flagged PARTIAL** (must annotate differently in JSON schema when implemented).
- **ADR-006** — Observability overlays cost fields already; ADR-007 adds policy, not tracing mechanics.
- **ADR-011** — Eval extraction should keep cost accounting portable.
