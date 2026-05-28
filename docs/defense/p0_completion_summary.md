# P0 construction — completion summary

All values below are extracted from frozen artifacts in the repo (no hand-typed gate numbers).

## Sprint overview (task-close tags)

Day 10 gate on frozen comparison: **GREEN** (all gated metrics at target).

| Task | Close date (UTC) | Commit (short) / subject |
|---|---|---|
| T-01 | 2026-05-01 | `fdb95fea` fix(T-01): align agents README to positioning vocabulary |
| T-02 | 2026-05-01 | `2937d36b` feat(T-02): wire CATALYST_DB_PATH and split dev/eval/demo DB |
| T-03 | 2026-05-01 | `d58fb06b` feat(T-03): ingestion runs/checkpoints tables + asset qualit |
| T-04 | 2026-05-02 | `d6dcfe15` feat(T-04): conditional fallback orchestration + cross-sourc |
| T-05 | 2026-05-02 | `2626a65c` feat: T-05 provider rate-limiter hardening + key pool config |
| T-06 | 2026-05-03 | `afaa34b0` feat(T-06): full 1-year backfill execution with gate-passing |
| T-07 | 2026-05-03 | `5f268ed5` docs(decision): W-15 defer T-07 vector index rebuild to P1 |
| T-08 | 2026-05-03 | `88ddbec3` feat(agents): T-08 validator, 4-state output, critic decisio |
| T-09 | 2026-05-03 | `4c339e81` feat(agents): T-09 deterministic DecisionRouter |
| T-10 | 2026-05-03 | `b66549dc` feat(agents): T-10 retrieval policy Layer 1 + Layer 2 |
| T-11 | — | (no task/T-11-close tag; trace in T-11 merge history) |
| T-12 | 2026-05-03 | `8ecad076` feat(eval+agents): T-12 direct_llm baseline + failure taxono |
| T-13a | 2026-05-04 | `8eadf725` feat(eval): T-13a preflight + golden-set freeze + SHA captur |
| T-13b | 2026-05-04 | `162300f7` feat(eval): T-13b frozen eval runs + threshold calibration |
| T-14 | 2026-05-04 | `d227bd4d` feat(eval): T-14 gate script, comparison writers, defense de |
| T-15 | 2026-05-04 | `e52c315b` feat(eval): T-15 verification audit + T-16 code freeze and d |
| T-16 | 2026-05-04 | `e52c315b` feat(eval): T-15 verification audit + T-16 code freeze and d |

## Gate results (frozen comparison)

- `frozen_ts`: `20260503_154053`
- `schema_version`: `1.0`

| Gate | Value |
|---|---|
| `cost_latency_reported` | `True` |
| `evidence_validity` | `1.0` |
| `schema_validity` | `1.0` |
| `should_refuse_hit_rate` | `1.0` |
| `trace_completeness` | `1.0` |

## Tier-A reproducibility

- Baseline `code_git_sha` (header): `9f844eddfffff83a22077eb5c9ea16025a148019`
- `db_sha256`: comparison + preflight agree with file fingerprint at freeze (3d8a1ee3a86b2167…)
- Tier-A rerun artifact: `data/eval_reports/rerun_20260504_072205_mcj_full.json`
- `status_mismatches`: **0** (empty list ⇒ per-case 4-state status parity vs frozen mcj_full)
- Distributions — baseline: `{'INSUFFICIENT': 3, 'PARTIAL': 2, 'SUFFICIENT': 5}`
- Distributions — rerun: `{'INSUFFICIENT': 3, 'PARTIAL': 2, 'SUFFICIENT': 5}`

## Frozen anchor summary

- **frozen_ts**: `20260503_154053` (from comparison header)
- **db_sha256**: `3d8a1ee3a86b216779dfd9cc2e6ccd45090326c65b08e4a19e7b89d25935e491`
- **lancedb_dir_sha256**: `DEFERRED_P1`
- **code_git_sha** (eval): `9f844eddfffff83a22077eb5c9ea16025a148019`
- **random_seed**: `42`
- **geo_corpus_tier**: `2`
- **model_id_per_role** (from comparison):
  - `critic`: `claude-sonnet-4-20250514`
  - `direct_llm`: `claude-sonnet-4-6`
  - `judge`: `claude-sonnet-4-20250514`
  - `validator`: `claude-sonnet-4-20250514`

### Git tags

- `task/T-13b-close` — eval freeze anchor (Tier-A runs)
- `task/T-14-close` — operational §J.6 line for **no further `packages/`** after close
- `defense-freeze-2026-05-04` — annotated; target commit **must** match `code_git_sha` above

### Preflight snapshot (`preflight_20260503_154053.json`)

- `pass`: `True`
- `schema_version`: `1.0`
- `geo_corpus_tier`: `2`
- `db_sha256`: `3d8a1ee3a86b216779dfd9cc2e6ccd45090326c65b08e4a19e7b89d25935e491`

### Freeze header (`freeze_header_20260503_154053.json`)

- Top-level `schema_version`: `1.0`.
- `artifacts`: relative paths to frozen DB, golden set, and preflight JSON.
- `header`: same shape as eval headers (`frozen_ts`, `db_sha256`, `geo_corpus_tier`, `lancedb_dir_sha256`, `case_distribution`, `model_id_per_role`, `random_seed`). The `code_git_sha` inside this file reflects the commit at **freeze-header capture** and may differ from the comparison header’s eval pin — use **`20260503_154053_comparison.json` → `header.code_git_sha`** for Tier-A defense tag alignment.

### Threshold calibration (`20260503_154053_threshold_calibration.json`)

- `chosen` thresholds: `{'K_partial': 2, 'K_sufficient': 4, 'M_threshold': 0.6}`
- `chosen_accuracy`: `1.0`
- `rationale` (excerpt): Selected the strictest top-scoring candidate within the OD-1 calibration window at accuracy=1.00 (matches current defaults).…

## Waiver summary (W-01 … W-15)

| Waiver | Status | Note |
|--------|--------|------|
| W-01 | Deferred P1 | `rag_only` baseline not in P0 matrix |
| W-02 | Deferred P2 | Layer 3 retrieval |
| W-03 | Deferred P2 | Critic LLM-graded sufficiency |
| W-04 | Deferred P2 | Budget circuit breaker |
| W-05 | Deferred P2 | Model routing |
| W-06 | Deferred P1 | LangSmith |
| W-07 | Roadmap | Full eval matrix |
| W-08 | Substituted | `should_refuse_hit_rate` for n=3 refusals |
| W-09 | Deferred P1 | GDELT corpus |
| W-10 | N/A | Not a distinct waiver id in plan §A.4 (numbering jumps W-09 → W-11) |
| W-11 | Conditional P1 | Cross-source semantic dedup if triggered |
| W-12 | Conditional | Backfill window reduction if triggered |
| W-13 | Dormant | Notebook fallback — not activated |
| W-14 | Deferred P1 | Two-level chunking + reranker + sentence-offset enforcement |
| W-15 | Active (disclosure) | LanceDB / vector retrieval deferred; SQL-only P0 eval |

## Known limitations / residual risks

- **W-15**: P0 eval uses **SQL-only** retrieval; `data/lancedb_gold/eval_frozen/` may be empty; gate treats Lance sentinel as warning.
- **T-01**: Product READMEs pass verbatim-phrase sweep; some planning/spec docs still mention the deprecated phrase inside documented verification text.
- **T-06**: Frozen `polygon_news` row count is below the plan’s nominal full-backfill band (~25k–60k); subset corpus is intentional for this defense slice (see audit).
- **§J.6**: `task/T-13b-close..HEAD` includes `packages/eval/` from T-14; **operational** freeze for new code is `task/T-14-close` (see `docs/testing/audit_2026-05-04.md`).

## Defense readiness

- **Artifacts**: see `docs/defense/artifact_inventory.md` (all §I.6 paths verified non-empty / JSON-valid where applicable).
- **Deck**: `docs/defense/deck.md` — `## Slide N:` count = **8** (target ≥8) + appendix present: **True**.
- **Rehearsals**: ≥2 end-to-end dry runs — **pending (human)**; record dates in audit when done.
