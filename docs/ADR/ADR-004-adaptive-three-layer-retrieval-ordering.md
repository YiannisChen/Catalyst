> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# ADR-004: Adaptive Three-Layer Retrieval Ordering (Direct → Macro → Related)

**Status:** Proposed  
**Date:** 2026-05-04  
**Decision:** Freeze **canonical adaptive ordering** **`Layer.DIRECT → Layer.MACRO → Layer.RELATED`**, **`full-version-execution-spec.md` §5** naming. **P1 activates hybrid (BM25 + vector + gated rerank) for DIRECT and MACRO only.** **`Layer.RELATED` remains unavailable until P2**, surfacing **`NotImplementedError`** from `retrieve()` as the defensive seam.

## Context

Adaptive multi-layer retrieval is the mechanism that keeps attribution **evidence-efficient** — cheap paths first, broaden only under Critic/policy pressure. Exec-spec Phase 2 and `docs/full-version-execution-spec.md` §5 define three conceptual layers (`direct → macro/market evidence → related-entity`). The shipped enum in **`catalyst_agents.retrieval.policy.Layer`** mirrors that taxonomy.

A historical inconsistency exists: **`docs/full-version-design-delta.md` §2** lists an alternate ordering of “peer” vs “market-wide macro” tiers. **`full-version-execution-spec.md`** and **`docs/plans/2026-05-04-p1-p2-architecture-plan.md`** are the canonical layer semantics for Catalyst P1/P2 execution; interpret design-delta diagrams only after mapping Layer numbers to **`DIRECT` / `MACRO` / `RELATED`**.

P0 (**W-15**) allowed SQL fallback when Lance directories are absent while keeping hybrid code wired for later activation (`policy.py:_hybrid_path` vs `_sql_fallback_query`).

## Decision

- **Canonical expansion order:** Always attempt **`DIRECT`** (ticker-filtered evidence) **before** **`MACRO`** (macro/geopolitical/market-correlated source types **without ticker filter**) **before** **`RELATED`** (peer/supplier/static-map expansion — **implementation deferred**).
- **P1 retrieval activation scope:** **`DIRECT`** and **`MACRO`** **`retrieve()` branches** MUST use **`lancedb_store.hybrid_search()`** when Lance is available (**`policy.py:_lancedb_available`** returns true).
- **P1 RELATED seam:** **`Layer.RELATED` callers must encounter `NotImplementedError("layer3_not_implemented")`** (defensive posture). Architecture plan assigns full RELATED implementation to **P2-T01** (`NotImplementedError` removal + static `PEER_MAP` behavior).
- **SQL fallback:** When Lance artifacts are absent, **SQL fallback remains mandatory** for DIRECT/MACRO to preserve regressions against frozen baselines (**W-15** behavior).
- **Bounded expansion constants:** Respect existing **`MAX_LAYERS_P0 = 2`** effective semantics through P1 (no RELATED traversal). Preserve **`MAX_EXPANSIONS`** wiring as exercised by miner/router tests.
- **Sufficiency probing:** Prefer rule-based **`check_sufficiency`** (mean score + count thresholds) consistent with **`docs/full-version-design-delta.md` §2** using **`rrf_score`** when rerank unavailable; when rerank is available, thresholds may also consult **`rerank_score`** after P1 calibration (note in P1-T12).

## Alternatives considered

- **Implement RELATED in P1:** Rejected — strategy spec positions Layer 3 for post-P0 roadmap phases; violates current waiver/task boundaries and dilutes statistical closure of P1.
- **Reorder macro before direct:** Rejected — contradicts cheapest-first mandate and hurts interpretability (“direct ticker evidence discarded”).
- **Remove `NotImplementedError` and silently return empty RELATED:** Rejected — masks missing capability; explicit failure is preferable for auditors and prevents false “layers_used” claims.

## Consequences

**Positive**

- Naming alignment with **`full-version-execution-spec.md` §15 ADR backlog entry** preserves executive traceability.
- P1 narrows blast radius — vector path shipped where data contracts already exist (`DIRECT`/`MACRO` source-type lists).

**Negative**

- Golden cases requiring RELATED peers remain structurally underserved until **P2-T01** (~60% hypothetical coverage gap framing in legacy design prose — quantify honestly in evaluation reports).

**Follow-up tasks**

- **P2-T01:** Replace RELATED stub with static peer-map retrieval merging into prior layers (`docs/plans/2026-05-04-p1-p2-architecture-plan.md` §2).
- Harmonize pedagogical diagrams in **`docs/full-version-design-delta.md`** in a docs sweep (non-blocking coding task).

## References

- `docs/full-version-execution-spec.md` — §§5 (layer definitions), §13 Phase 2, **§15 (ADR backlog slot ADR-004)**.
- `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` — §3.2 Layer 3 short-circuit policy for P0 (RELATED semantics vs router).
- `docs/full-version-design-delta.md` — tiered retrieval narrative (§2) — interpret with canonical mapping caveat above.
- `docs/decisions/W-15-t07-deferred-to-p1.md` — SQL-vs-Lance gating mandates.
- `docs/plans/2026-05-04-p1-p2-architecture-plan.md` — **P1-T04** gates and **P2-T01 RELATED** note.

## Implementation notes

- **Policy module:** `packages/agents/catalyst_agents/retrieval/policy.py` — enums `Layer`, constants `MACRO_SOURCE_TYPES`/`DIRECT_SOURCE_TYPES`, branching in `retrieve()`, hybrid vs SQL selection.
- **Integration surface:** Lance path calls `hybrid_search(...)`; SQL path queries `clean_assets`.
- **`RetrievalMetadata.stop_reason`** already records `"layer3_not_implemented"` when RELATED is invoked.

### Related ADRs

- **ADR-002** — Hybrid fusion prerequisite.
- **ADR-008** / **ADR-009** — Data plane feeding hybrid path.
- **ADR-007** (deferred P2) — Budget + routing may cap expansions orthogonal to ordering.
- **ADR-010** — Eval subsets must disclose when cases require RELATED semantics not yet shipped.
