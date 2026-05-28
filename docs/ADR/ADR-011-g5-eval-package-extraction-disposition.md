> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# ADR-011: G5 Eval Package Extraction Disposition (`packages/eval`)

**Status:** Proposed *(decision recommendation subject to coupling audit outcome — default stance below)*  
**Date:** 2026-05-04  
**Decision:** Frame **G5** per **`docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` §2.1–2.2** as extracting **`packages/eval`** into an installable harness with **`pyproject.toml` already present**.

## Context

Teacher Mac framing positions reusable packages as appreciating assets versus one-off glue code. **`packages/eval`** already exposes metrics, schemas, comparison writers, baseline harnesses, scripts, golden sets (`pyproject.toml` package **`catalyst_eval`**). Hooks into full-system runs (**`packages/eval/scripts/run_frozen_eval.py`**) intentionally import **`catalyst_agents.graph`**, miner dating helpers, retrieval policy enums, **`TraceWriter`**, and exporters — tying evaluation to the integrated stack.

**Architecture plan P1-T15** mandates an explicit **`go`** / **`defer-to-P2`** / **`no-go`** decision referencing coupling depth.

### Decision criterion matrix

| Outcome | Condition | Routing |
|---------|-----------|---------|
| **Go (P1)** | Harness **core surfaces** (**metrics/schema/reports/unit tests**) exhibit **zero mandatory imports** of **`catalyst_agents`** / **`catalyst_data`** AND thin integration scripts can move behind optional extras/README instructions without losing CI signal. | Land extraction scaffolding in **P1** backlog explicitly. |
| **Go (P2)** | Separation needs medium refactor (shared DTO duplication, relocating scripts, rewriting runner dependency injection). | Bind to **P2-T05**/dedicated **`P2-T05b`** per plan cross-links. |
| **No-go** | Finance attribution domain coupling is inseparable **without rewriting mission** (“finance-agnostic harness” invalidated). | Document post-v1 aspiration; keep monorepo eval as canonical. |

### Provisional engineering assessment (executor note — reversible)

- **Pure-package audit (`catalyst_eval` library tree):** **`packages/eval/catalyst_eval/**`** appears to contain no direct imports of `catalyst_agents` / `catalyst_data` based on preliminary inspection — formal grep audit required per checklist below before status advances to ACCEPTED.
- **Script-level coupling hotspots:** **`packages/eval/scripts/run_frozen_eval.py`** imports **`catalyst_agents.*`** broadly; **`packages/eval/tests/test_direct_llm_baseline.py`** imports **`catalyst_data.storage.sqlite`**.
  - Interpretation: **default disposition = Go (P2)** unless P1 allocates time to refactor runner boundaries — consistent with Teacher Mac thrift vs defense schedule risk.

Formal sign-off adjusts the matrix row after deterministic audit enumeration (import graph + layering diagram).

### Coupling audit checklist (must complete before ACCEPTED)

| Check | Evidence artifact |
|-------|-------------------|
| Grepped imports `catalyst_agents` / `catalyst_data` under `packages/eval/**` categorized by **runtime core** vs **scripts/tests** vs **contrib** |
| Mapped public API promises (`metrics`, `schema`, `harness.runner`, frozen eval entrypoints) |
| Documented acceptable **optional extras** (`pip install catalyst-eval[inprocess]`) separating heavy integration |
| Verified `catalyst_eval/harness/frozen_eval.py` and `catalyst_eval/harness/runner.py` import trees specifically (highest coupling risk in library surface) |
| Confirmed **`packages/eval/golden_set` licensing + distribution** unaffected |
| Verified CI story (unit tests mocking heavy deps vs integration job monorepo-only) |

### Alternatives considered

- **Mandatory immediate extraction in P1:** Rejected absent schedule proof — violates risk-managed defense sequencing.
- **Delete G5 aspiration:** Rejected — discards reusability ethos without replacement narrative.
- **Vendor agents into eval package:** Rejected — inverts dependency direction, balloons distribution.

### Consequences

**Positive**

- Clear decision tree prevents ambiguous half-extractions burning calendar.

**Negative**

- Maintaining duplicate interfaces across phases if partial extraction mishandled — mitigate via façade modules documented in README.

### Follow-up tasks

- Execute checklist, attach summary to **`docs/decisions/`** or future closure memo.
- Update packaging metadata / optional dependency tables after chosen path.

### References

- `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` — §§2.1–2.2 (G5 description, cuttable wording), Day 10 pointer lines.
- `docs/plans/2026-05-04-p1-p2-architecture-plan.md` — **§1.2 P1-T15**, **DAG node ADR-011**, **§2 cross-link to P2-T05**.
- `docs/full-version-execution-spec.md` — evaluation redesign cross-reference (§12) reinforcing harness ownership boundaries (orthogonal detail).

### Implementation notes

- **Layout:** `packages/eval/catalyst_eval/{metrics,harness,schema,reports}` — bounded surfaces for extraction polishing.
- **Integration seam:** Thin adapter module pattern recommended (future **`catalyst_eval.contrib.catalyst`** or sibling) isolating **`build_attribution_graph`** dependencies.
- Scripts/tests enumerated during audit supersede illustrative paths here.

### Related ADRs

- **ADR-006** — Tracing exporters live in **`catalyst_agents`** today; relocating affects integration packaging.
- **ADR-008–010** — Eval artifacts depend on reproducible embeddings, retrieval behavior, datasets — extraction must preserve manifest headers.
