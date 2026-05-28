> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# ADR-010: Golden Set Expansion and Statistical Power (P1 Sample Protocol)

**Status:** Proposed  
**Date:** 2026-05-04  
**Decision:** `v1_2_p1_set.jsonl` MUST remain a strict superset of `v1_2_p0_set.jsonl` with byte-identical first ten records. Target **`≥30` total cases**. Reporting MUST pair **Wilcoxon** (or related paired tests on the expanded set) with **honest small-sample disclaimers**, **preserve immutable P0 gate baselines**, and **explicitly forbid sample manipulation solely to chase passing gates.**

## Context

Frozen P0 defenses relied on **`packages/eval/golden_set/v1_2_p0_set.jsonl`** (10 cases — distribution **5 × SUFFICIENT, 2 × PARTIAL, 3 × INSUFFICIENT / should-refuse family** depending on evaluator mapping). Statistical comparison between **`direct_llm`**, imminent **`rag_only`**, and **`mcj_full`** requires more than heroic effect sizes credible at **n = 10** for Wilcoxon-style paired analyses.

Architecture plan §1.2 **P1-T10 / P1-T11** mandates **immutable P0 sample** semantics while enabling expanded coverage drawn from annotated **`golden_set/v1_2.jsonl`**.

### Decision

1. **`v1_2_p1_set.jsonl` superset invariant:** Every record from `v1_2_p0_set.jsonl` appears verbatim at the beginning of `v1_2_p1_set.jsonl` (first 10 lines byte-identical per plan wording). Append-only augmentation after line 11+.
2. **Sample size guideline:** Operational target **minimum 30** paired observations Post-expansion (**architecture plan cites ~27** computational floor for modest effect sensitivity at usual α/power — round to ≥30 pragmatically).
3. **Sampling ethics:** Forbidden practices:
   - Reordering/removing/editing legacy P0 cases to alter gate outcomes (**immutable P0 gates** doctrine).
   - Overfitting prompts/threshold retrieval knobs **until gates flip** absent documented calibration protocol + separate holdout rationale.
   - Presenting aggregate improvements without concurrently publishing **P0-subset sliced metrics** (**apples-to-apples**) vs full-set uplift.
4. **Statistical framing:**
   - **n = 10:** Wilcoxon can only stabilize detection for **large** paired shifts; qualitative conclusions remain exploratory unless effect dominates noise.
   - **n ≥ ~30:** Wilcoxon (and supporting bootstrap sensitivity analyses) gains defensibility **but still documents category stratification variance** (`docs/full-version-design-delta.md` §8 spirit).
   - Prefer reporting **paired effect sizes**, **confidence intervals**, and narrative caveats tied to refusal subsets.
5. **Category coverage:** Expanded cases SHOULD span **earnings**, **macro**, **sector rotation / cross-name** horizons described in **`docs/full-version-design-delta.md` §2** coverage bullet list — prioritized when annotation labor permits rather than artificially balancing classes if fidelity suffers.
6. **Annotation quality gate:** Candidate cases that fail inter-annotator or self-consistency review are excluded from `v1_2_p1_set.jsonl` rather than force-included — the ≥30 target is a guideline, not a mandate to sacrifice annotation fidelity.
7. **Gate interaction:** Automated gate scripts (**`packages/eval/scripts/check_p0_gate.py`**) must continue evaluating **immutable P0 baselines**. P1 regressions flagged when **subset metrics** degrade — **investigate causally**, never mutate historical rows.

### Alternatives considered

- **Throw away P0 10-case lineage:** Rejected — violates frozen-sample / defense continuity.
- **Stop at n = 15 opportunistically:** Rejected — insufficient power uplift vs engineering cost of dataset QA.
- **Permit dataset edits gated by “engineering convenience”:** Rejected — compromises integrity narrative.

### Consequences

**Positive**

- Honest differentiation vs **`direct_llm`** becomes statistically narratable rather than vibes-only.

**Negative**

- Annotation latency dominates schedule — mitigated via staged rollout (start from highest-signal untouched events).

**Follow-up tasks**

- Produce **`v1_2_p1_set.jsonl`** with expansion manifest + curator notes (**P1-T10**).
- Extend experiment runner slicing logic (**P1-T11**) to emit **`p0_subset` vs `full_set`** comparisons.

### References

- `docs/plans/2026-05-04-p1-p2-architecture-plan.md` — **§0 Frozen-sample protocol**, **§1.2 P1-T10/P1-T11 acceptance**, **DAG ADR bindings**.
- `docs/full-version-execution-spec.md` — §§12–13 evaluation redesign + phase ordering (**§15 pointer for orthogonal ADRs**, not duplication).
- `docs/full-version-design-delta.md` — §§2, 8 (baseline experiments framing).
- `docs/full-version-design-delta.md` — strategy immutability echo in architecture plan (**策略文档 §6.1 rule** shorthand in plan preamble).

### Implementation notes

- **Golden assets:** `packages/eval/golden_set/v1_2_p0_set.jsonl` (immutable reference), **`v1_2.jsonl` pool**.
- Harness entrypoints: **`packages/eval/scripts/run_experiments.py`** (architecture plan cite) / existing frozen eval scaffolding.
- **Preflight tooling:** **`packages/eval/scripts/preflight.py`** lineage per prior freeze docs.

### Related ADRs

- **ADR-004** — Cases requiring RELATED peers remain labeled honestly until P2.
- **ADR-005** — Status downgrade semantics underpin gate metrics interpreting PARTIAL spikes.
- **ADR-011** — Extraction feasibility interacts with harness modularity enforcing dataset rules.
