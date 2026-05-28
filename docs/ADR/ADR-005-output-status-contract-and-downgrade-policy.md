> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# ADR-005: Output Status Contract and Downgrade Policy (P0 Retrospective)

**Status:** Proposed *(retrospective documentation of behaviors already shipped in P0)*  
**Date:** 2026-05-04  
**Decision:** Preserve the **four-valued final output enum** and the **validator-first downgrade ladder** summarized below as the audited contract underpinning defenses, frozen gates, and trace semantics.

## Context

Exec-spec **`full-version-execution-spec.md` §§6–8** mandates explicit output states plus a validator pass after synthesis. Catalyst P0 wired Miner → Critic → DecisionRouter → Judge → **Validator** → Finalizer against the **`catalyst_eval_frozen.db`** corpus with SQL retrieval under **W-15**. This ADR records **why** downgrade choices look the way they do in code (`validator.py`, `finalizer.py`) rather than proposing new behavior.

## Decision

Final outputs use **`OutputStatus`** with exactly four literals (string Enum values align with downstream JSON reporting):

| State | Meaning (contract) |
|-------|---------------------|
| **SUFFICIENT** | Attribution meets sufficiency thresholds with validator-clean evidence grounding. |
| **PARTIAL** | Partial explanatory coverage OR enforcement downgrades from optimistic success to constrained success. |
| **INSUFFICIENT** | Expansion/refusal path concludes without satisfying evidence after policy exhaustion (data-gap refusal family). |
| **SYSTEM_ERROR** | Execution / infra / model-timeout class failures — orthogonal to deliberate evidence refusal semantics. |

**Validator responsibilities (mirror `packages/agents/catalyst_agents/nodes/validator.py`):**

1. **Grounding gates:** Evidence IDs exist in retrieval sets; evidence dates obey configured window versus trade date.
2. **Schema gate:** Structured causes + Markdown summary obey `ValidatedJudgeOutput` Pydantic schema.
3. **Magnitude sanity gate:** Cannot claim **`SUFFICIENT`** if Critic `magnitude_coverage` fails **`M_THRESHOLD`** while sufficiency-derived status would otherwise be **`SUFFICIENT`**.

**Status derivation prior to downgrade:** Derived from **`CriticDecision.sufficiency`** when present (`sufficient`→`SUFFICIENT`, `partial`→`PARTIAL`, otherwise `INSUFFICIENT`); if absent, falls back to presence of graded causes.

**Validator downgrade ladder (already implemented):**

- On failure with **LLM available:** issue **exactly one** structured correction (`invoke_with_retries`).
  - Correction passes → restored status uses `_status_from_state` mapping.
  - Second failure → `_downgraded_status(second_failure)`:
    - **`model_timeout`** ⇒ **`SYSTEM_ERROR`**
    - All other enumerated failures ⇒ **`PARTIAL`** (including missing evidence IDs, temporal violations, schema violation, magnitude failure).
- On failure **without** correction LLM (e.g., harness injecting `llm=None`) → immediate downgrade via `_downgraded_status(failure)` with same **`model_timeout`** vs **`PARTIAL`** split.
- **RuntimeError surfacing validator correction timeout** ⇒ **`SYSTEM_ERROR`** with **`validation_error="model_timeout"`**.

**Finalizer (`finalizer.py`)** normalizes statuses when downstream nodes omit them:

- Respect existing `state["output_status"]` first.
- Otherwise branch on **`error_type == "system_error"`**, **`critic_decision`**, or presence of **`causes`**.

### Alternatives considered (historical)

- **Treat schema violations as `INSUFFICIENT`:** Rejected — conflates data refusal with malformed LLM payloads; downgrade path keeps refusal taxonomy separate from structural failures.
- **Infinite correction loops:** Rejected — unbounded tokens; single retry aligns with **`full-version-execution-spec.md` §8**.
- **Collapse PARTIAL into INSUFFICIENT:** Rejected — blurs calibrated intermediate quality needed for attribution F1 calibration and tiered reporting.

## Consequences

**Positive**

- Committee narrative “refuses cleanly vs fails loudly” hinges on deterministic boundaries above.
- Trace rows capture validator-driven transitions for regression (`agent_runs.status`, event `status_before/after`).

**Negative**

- **`PARTIAL` unions multiple semantics** — evidence-limited explanations share label with grounding downgrades unless logs disambiguated via `validation_error` / phase metadata.

**Follow-up tasks**

- P1 (**design delta §3**) expands **error taxonomy** via `error_log` — routing must preserve non-conflation guarantees from **`full-version-execution-spec.md` §§6–9**.
- Report writers should surface **`validation_error`** fields when interpreting PARTIAL dominance shifts.

## References

- `docs/full-version-execution-spec.md` — §§6–9 (DecisionRouter ladder, validator contract, harness taxonomy scaffolding).
- `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` — §§1–3 (explicit four-state refusal claim).
- `docs/full-version-design-delta.md` — §3 (future error-log split — forward pointer only).
- `docs/full-version-execution-spec.md` **§15** — reserved backlog slot definition for ADR-005 (output status downgrade policy narrative).

## Implementation notes

- **Validator logic:** `packages/agents/catalyst_agents/nodes/validator.py` — `_validation_failure`, `_downgraded_status`, `_status_from_state`, correction prompt assembly.
- **Finalizer normalization:** `packages/agents/catalyst_agents/nodes/finalizer.py`.
- **Tests:** `packages/agents/tests/test_validator.py`, `packages/agents/tests/test_failure_taxonomy.py`.
- Related threshold constants originate in **`nodes/critic.py`** (`K_PARTIAL`, `K_SUFFICIENT`, `M_THRESHOLD`).

### Related ADRs

- **ADR-004** — Retrieval layering influences whether validator sees complete evidence lookups.
- **ADR-006** — Trace captures validator status transitions keyed by **`trace_id`**.
- **ADR-007** (P2) — Budget overflow must map to **`PARTIAL` with explicit budget semantics** without colliding with validator downgrade taxonomy.
- **ADR-011** — Eval harness parses statuses for metrics gates.
