# S2: Harness Honesty — Implementation Plan (AMENDED v3)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** MAKE expand_macro live (CRAG corrective-retrieval), establish single-source PolicyConfig (pydantic BaseModel, dumped into agent_runs.config), add RunBudget breaker, fail-fast on unknown model pricing, enforce A6 data-plane isolation.

**Architecture:** PolicyConfig is a pydantic BaseModel with explicit v1 values including temporal_penalty=0.9 (verified at critic.py:231). v1 max_expansions=0 structurally disables Row 6. expand_macro trigger is a decision_router Row 6 intercept on sufficiency != "sufficient" AND DIRECT AND expansions_used < max_expansions. max_expansions sourced from PolicyConfig only (DELETE module-level MAX_EXPANSIONS=2 fallback at decision_router.py:59).

**Tech Stack:** Python 3.12+, sqlite3, langgraph, LanceDB. Pydantic for PolicyConfig (not frozen dataclass).

**Design Refs (normative):** A1, A2, A3, A6, §0.2, §0.3.

**Process rule:** CLAUDE.md for writing-plans: strictly exclude all code blocks.

---

## Phase 0: Real-Run Verification Evidence

### 0.1: Current K/M values
critic.py:26-28 — K_SUFFICIENT=1, K_PARTIAL=1, M_THRESHOLD=0.3 ✓

### 0.2: Temporal penalty verified
critic.py:231 — `rel = max(0.0, rel * 0.9)` when temporal_match is False. v1 MUST set temporal_penalty=0.9 or the no-behavior-drift replay fails. ✓

### 0.3: MAX_EXPANSIONS fallback verified
decision_router.py:59 — `max_expansions = int(state.get("max_expansions", MAX_EXPANSIONS) or MAX_EXPANSIONS)` where MAX_EXPANSIONS=2.
This fallback bypasses PolicyConfig. Must be DELETED; source max_expansions from PolicyConfig only. ✓

### 0.4: MACRO_SOURCE_TYPES verified
policy.py:27-35 — MACRO_SOURCE_TYPES = (macro_news, market_news, geopolitical_news, fred_rates, fred_macro, gdelt_news, policy_news). Does NOT include fmp_fundamentals (which is in DIRECT_SOURCE_TYPES at line 20). Adding fmp_fundamentals to MACRO would pull other companies' fundamentals (MACRO drops ticker filter). ✓

### 0.5: Source tier distribution
Tier 2: 12, Tier 3: 1,962, Tier 4: 26,916, Tier 5: 7,971. tier_weights must cover 1–6 (T5 has live data; missing key crashes). ✓

### 0.6: ADR-010 M_THRESHOLD
ADR-010-critic-k-sufficient-recalibration.md:18 — "M_THRESHOLD remains 0.6". The plan's "0.45 from ADR-010" was wrong. 0.45 is scheme-C env value, needs human sign-off (Decision #4). ✓

---

## Dependencies & CORE-vs-STRONG Boundary

- **Dependencies:** S1 (metrics). Gated by S4 regression.
- **CORE:** Phases 1–4.
- **STRONG:** Phases 5–6.

---

## Phase 1: PolicyConfig Single Source + No-Behavior-Drift

**Design refs:** A2, §0.2, §0.3.

### PolicyConfig Schema (AMENDED — pydantic BaseModel, all fields explicit for v1)

| Field | Type | v1 Value | Description |
|---|---|---|---|
| K_sufficient | int | 1 | Evidence count threshold |
| K_partial | int | 1 | Evidence count for partial |
| M_threshold | float | 0.3 | Magnitude coverage threshold |
| tier_weights | dict[int→float] | {1:1.0,2:1.0,3:1.0,4:1.0,5:1.0,6:1.0} | Covers tiers 1–6 (T5 has 7,971 live articles) |
| temporal_mode | Literal["soft","hard"] | "soft" | Per §0.2 |
| temporal_penalty | float | 0.9 | Multiplier for temporal-mismatched chunks (critic.py:231 verified) |
| date_window_days | int | 3 | Retrieval date window |
| top_k_retrieval | int | 20 | Chunks per layer |
| top_k_reranked | int | 8 | Chunks after reranking |
| max_expansions | int | 0 | 0 = disabled (structural Row-6 guard) |
| schema_version | int | 1 | Policy schema version |
| relevance_threshold | float | 0.25 | Minimum relevance score |

### Removed Field Defaults
temporal_penalty has NO schema default — every version must set it explicitly. This prevents silent drift.

### Fields MOVED to RunBudget
max_cost_usd, max_llm_calls, max_wall_seconds

### Fields DELETED
CATALYST_M_THRESHOLD, _effective_m_threshold, budget_limit_usd

### Reproducibility
PolicyConfig serialized as JSON into agent_runs.config.

### v1 No-Behavior-Drift Guarantee
max_expansions=0 is structural: Row 6 condition `expansions_used < max_expansions` = `0 < 0` = False. Row 6 CANNOT fire under v1, regardless of sufficiency. temporal_penalty=0.9 matches critic.py:231 exactly.

### Source max_expansions from PolicyConfig Only (AMENDED — fix #7 should-fix)
DELETE the module-level MAX_EXPANSIONS=2 at decision_router.py:7. DELETE the state.get("max_expansions", MAX_EXPANSIONS) fallback at decision_router.py:59. max_expansions comes from PolicyConfig injected into state at graph-build. Only fallback is in test fixtures where PolicyConfig is provided.

TEST INTENT:
1. PolicyConfig.v1() matches all v1 values exactly
2. No-behavior-drift replay: ALL P0 cases (including 3 unanswerable) → identical output_status + cause_texts
3. temporal_penalty=0.9 explicitly in v1 (not from default)
4. tier_weights covers {1,2,3,4,5,6} → no KeyError on T5 articles
5. **State dict without max_expansions under v1 PolicyConfig → Row 6 still cannot fire (no module fallback)**

**Files:**
- Create: catalyst_agents/policy_config.py
- Modify: critic.py, decision_router.py, graph.py

---

## Phase 2: MAKE expand_macro Live

**Design refs:** A1. CRAG corrective-retrieval pattern.

### Decision Table: DecisionRouter (COMPLETE)

Inputs: sufficiency (str), current_layer (Layer), expansions_used (int), max_expansions (int from PolicyConfig), graded_evidence (list), budget_exceeded (bool)

| # | Priority | Condition | Route | router_reason |
|---|---|---|---|---|
| 0 | highest | budget_exceeded AND causes exist | finalizer → PARTIAL | budget_exceeded |
| 0b | highest | budget_exceeded AND no causes | insufficient_handler → INSUFFICIENT | budget_exceeded |
| 1 | system | error_type == "system_error" | system_error | upstream_system_error |
| 2 | system | ticker_consistent == False | insufficient | ticker_mismatch_guard |
| 3 | system | market_session_valid == False | insufficient | market_session_guard |
| 4 | system | magnitude_plausible == False | insufficient | magnitude_guard |
| 5 | system | critic_decision is None | insufficient | missing_critic_decision |
| 6 | expansion | sufficiency != "sufficient" AND current_layer == DIRECT AND expansions_used < max_expansions | expand_macro | sufficiency_triggered_expansion |
| 7 | guard | next_action == "proceed" AND graded_evidence is empty | insufficient | empty_graded_evidence_guard |
| 8 | normal | next_action == "proceed" | judge | critic_proceed |
| 9 | expansion | next_action == "expand_macro" AND expansions_used < max_expansions | expand_macro | critic_expand_macro |
| 10 | exhausted | next_action == "expand_macro" AND expansions_used >= max_expansions | insufficient | expansions_exhausted |
| 11 | normal | next_action == "refuse" | insufficient | critic_refused |
| 12 | normal | next_action == "expand_related" | insufficient | layer3_not_implemented |
| 13 | fallback | (all else) | insufficient | critic_refused |

### Notes on Rows 0/0b
Rows 0/0b are effectively dead at the router (causes can't exist pre-Judge). They exist for defense-in-depth; in practice the budget breaker fires at Critic/Judge entry, not here. Keep for completeness; document as "dead-at-router, budget enforced at node-entry."

### v1 → v2 Mechanism
- v1: max_expansions=0 → Row 6 `0 < 0` = False → structurally impossible
- v2: max_expansions=2 → Row 6 `0 < 2` = True → fires on non-sufficient DIRECT

### MACRO Layer Source Types (AMENDED — fix #8 should-fix)
Use existing MACRO_SOURCE_TYPES from policy.py:27-35 (fred_macro, macro_news, market_news, geopolitical_news, fred_rates, gdelt_news, policy_news). Do NOT add fmp_fundamentals — it is company-specific (DIRECT_SOURCE_TYPES) and MACRO drops the ticker filter, so it would pull other companies' financials.

TEST INTENT:
1. v1 replay: all P0 cases → expand_macro NEVER fires, output matches recorded
2. v2 (max_expansions=2): low-relevance DIRECT → Row 6 fires → expand_macro → second miner invocation
3. After expansion, current_layer=MACRO → Row 6 does NOT re-fire
4. max_expansions exhausted → Row 10 → insufficient
5. MACRO source types do NOT include fmp_fundamentals
6. No module-level MAX_EXPANSIONS fallback exists

**Files:**
- Modify: decision_router.py, retrieval/policy.py, miner node, graph.py

---

## Phase 3: RunBudget Breaker

### RunBudget Schema
| Field | Type | Default |
|---|---|---|
| max_cost_usd | float | required |
| max_llm_calls | int | required |
| max_wall_seconds | float | required |
| spent_cost_usd | float | 0.0 |
| calls_made | int | 0 |
| started_at | float | now |

### Check Points
DecisionRouter entry (Rows 0/0b), Critic entry, Judge entry, Validator entry.

### Downgrade
causes non-empty → PARTIAL, router_reason="budget_exceeded"
causes empty → INSUFFICIENT, router_reason="budget_exceeded"
NEVER SYSTEM_ERROR

TEST INTENT: as in v2.

**Files:**
- Create: catalyst_agents/run_budget.py
- Modify: cost_tracker.py, graph.py

---

## Phase 4: Unknown Model Pricing — Fail Fast

### Contract
Unknown model_id → raise UnknownModelPricingError at graph-build. Free models: explicit 0.0 listing. usage_missing flag for missing usage data.

TEST INTENT: as in v2.

**Files:**
- Modify: cost_tracker.py, graph.py

---

## Phase 5: A6 Data-Plane Isolation

### Contract
_lancedb_dir_from_env() → Path | None. None → RuntimeDependencyLoader fails with clear error. Docstring notes removal of CATALYST_ALLOW_SQL_FALLBACK opt-in (the design bans silent fallback). Never touches _graph_factory, get_credential_store, get_live_run_service, build_llm.

**Files:**
- Modify: dependencies.py

---

## Phase 6 (STRONG): A4 Structured Outputs

## Phase 7 (STRONG): A5 Hard Temporal Gate (reads corpus_items, not clean_assets)

---

## Post-Implementation Verification

1. ALL P0 cases replay identically under v1 (including 3 unanswerable)
2. expand_macro PRESENT, structurally disabled under v1
3. expand_macro fires under v2, ≥2 miner invocations
4. temporal_penalty=0.9 explicit in v1 config
5. No module-level MAX_EXPANSIONS fallback exists
6. MACRO source types exclude fmp_fundamentals
7. tier_weights covers 1–6
8. UnknownModelPricingError raised (not zero)
9. Budget exceeded → PARTIAL or INSUFFICIENT, never SYSTEM_ERROR
10. policy serialized into agent_runs.config
