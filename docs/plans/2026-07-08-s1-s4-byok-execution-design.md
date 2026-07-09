# S1-S4 + BYOK Execution Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this design task-by-task. Do not commit, push, or stage without explicit user authorization.

**Goal:** Stabilize the current S1/S3 work, produce a real S1 baseline, implement S4 as a zero-LLM regression gate, execute S2 under that gate, and then run BYOK without mixing commit boundaries or damaging frozen/dev data.

**Architecture:** Treat S1 as the evaluator/ruler, S3 as the data surface repair, S4 as a deterministic regression gate built from S1 artifacts, and S2 as the behavior-changing harness work that must be measured by S4. Keep the 8GB Mac path API-only and SQL-only; defer S5 embedding/GPU work to a GPU box. Preserve the existing project commit taxonomy: C2 is S1, C3 is docs/research, S3 gets its own later commit.

**Tech Stack:** Python 3.12/3.13 venv, pytest, SQLite, JSON eval cache, Catalyst agents/eval/data-core packages, API LLM providers, no local embedding build on the Mac.

---

## 0. Current Verified State

### S1 eval ruler

- S1 code is mostly implemented: new metrics, judge cache, v1_3 answerable/unanswerable goldens, and three-arm harness exist.
- S1 is not commit-ready because deleted theater metrics still have live references.
- Verified breakage classes:
  - `scripts/run_experiments.py` imports deleted `AttributionF1`, `GroundingRate`, and `TemporalPrecision`.
  - `packages/agents/tests/test_run_experiments.py` collection fails through `scripts/run_experiments.py`.
  - `packages/eval/tests/test_compute_r1_correlation.py`, `packages/eval/tests/test_prepare_r1_external_chunks.py`, and `packages/eval/tests/test_run_r1_external_baseline.py` load missing legacy report scripts.
  - Legacy/demo scripts such as `scripts/e2e_drill.py`, `scripts/e2e_real.py`, `scripts/e2e_strict.py`, `scripts/event_case_study.py`, and `scripts/f1_threshold_sensitivity.py` still reference deleted theater metrics; decide archive vs port before claiming package health.

### S1 baseline

- No usable S1 baseline runner exists yet.
- `packages/eval/scripts/run_s1_baseline.py` is missing.
- `packages/eval/scripts/run_frozen_eval.py` exists but targets the old v1_2/frozen-eval contract and is not the S1 v1_3 baseline runner.
- Baseline must be SQL-only/API-only on the 8GB Mac; it must not build embeddings or load local rerankers.

### S3 data belt

- S3 Round 2 is functionally acceptable for dev/live data:
  - `corpus_items` view in `data/catalyst_dev_ws4b.db` has the corrected `a.title || char(10) || COALESCE(a.description, '') AS content_md`.
  - The old `CASE WHEN a.description IS NULL OR a.description = ''` view body is gone from dev DB.
  - `packages/agents/catalyst_agents/retrieval/policy.py` falls back to `clean_assets` only when `corpus_items` view is absent, not when it is empty.
  - `packages/data-core/catalyst_data/migrations.py` now guards frozen paths before `PRAGMA user_version`.
  - `packages/data-core/catalyst_data/storage/sqlite.py` drops and recreates `corpus_items` for non-frozen DBs.
- Frozen DB byte SHA changed from `0dfc81b154a9a2204bf8eec138efc1d76d4c3820bbc5fcca88b832d357ecdcdf` to `0d97a7ec61b6ec8fb5f9263b0b37b0efc9755812739d7afead7f720c3567e8dd`.
- Frozen DB content is equivalent enough for eval use:
  - `clean_assets` counts: `fmp_fundamentals=3500`, `fred_macro=3500`, `polygon_news=3125`, `polygon_ohlcv=3349`, total `13474`.
  - `sum(length(content_md)) = 66435087`.
  - No `articles`, `article_tickers`, `filings`, `filing_documents`, or `corpus_items` schema exists in frozen DB.
  - `PRAGMA integrity_check` and `PRAGMA quick_check` are `ok`.
  - File is `chmod 444`.
- No standalone incident document is required unless the user asks. Baseline metadata must record current SHA plus the content fingerprint to avoid ambiguity.

### S2 harness

- Plan exists at `docs/plans/2026-07-07-ws4b-s2-harness-honesty.md`.
- Not executed.
- Main landmines:
  - Make `expand_macro` live; do not delete it.
  - `PolicyConfig.v1()` must explicitly set `temporal_penalty=0.9` and `max_expansions=0`.
  - Unknown model pricing must raise on judge/eval paths.
  - Remove `MAX_EXPANSIONS` fallback sites in `decision_router.py`.
  - A6 eval/live data isolation must be additive and must not break existing dependency wiring.
  - No behavior drift for refusal/unanswerable cases.

### S4 regression gate

- Plan exists at `docs/plans/2026-07-07-ws4b-s4-regression-gate-recalibration.md`.
- Not executed.
- Split into two phases:
  - S4 Core Gate before S2: read committed S1 baseline/cache, zero live LLM, corrupted baseline red.
  - S4 Recalibration after S2: compare measured deltas and produce ADR-012.

### BYOK

- C1 BYOK base is committed.
- Plan exists at `docs/plans/2026-07-08-byok-multi-provider-model-layer.md`.
- Not executed.
- Must wait until after S2 because BYOK Task 4 overlaps S2 Phase 6 and BYOK cost behavior touches the same model/cost surfaces.
- Connectivity amendment is useful but not a blocker for S1 baseline if shell proxy env is sufficient.

---

## 1. Corrected Execution Workflow

### Phase A: Git and S1 cleanup

1. Do not commit current mixed tree as-is.
2. Fix S1 collection blockers:
   - Either archive/remove `scripts/run_experiments.py` and `packages/agents/tests/test_run_experiments.py`, or port them to S1 metrics. Preferred: archive/remove if superseded by S1 three-arm and no caller depends on them.
   - Remove or archive the three orphan R1 tests that load missing scripts; do not create fake scripts just to satisfy stale tests.
   - Archive or port legacy scripts importing deleted theater metrics. Preferred: archive old demo/e2e scripts if not part of current product flow.
3. Run S1-focused tests:
   - `.venv/bin/python -m pytest packages/eval/tests/test_s1_rebuild_eval_ruler.py packages/eval/tests/test_s1_three_arm.py packages/eval/tests/test_metrics.py packages/eval/tests/test_harness.py -q`
   - `.venv/bin/python -m pytest packages/agents/tests/test_adapter.py packages/agents/tests/test_trace.py -q`
4. Run package collection checks after cleanup:
   - `.venv/bin/python -m pytest packages/eval -q`
   - `.venv/bin/python -m pytest packages/agents -q`
5. Commit boundaries after tests:
   - C2: S1 eval ruler and required S1 cleanup.
   - C3: docs/plans and docs/research from the existing staged doc set.
   - C4: S3 data-belt remediation.

### Phase B: S1 baseline runner and artifact commit

1. Create minimal runner `packages/eval/scripts/run_s1_baseline.py`.
2. Runner inputs:
   - `--answerable packages/eval/golden_set/v1_3_answerable.jsonl`
   - `--unanswerable packages/eval/golden_set/v1_3_unanswerable.jsonl`
   - `--frozen-db data/catalyst_eval_frozen_v2.db`
   - `--trace-db .local/s1_baseline_traces.db`
   - `--cache-dir packages/eval/eval_cache`
   - `--limit N` for smoke.
   - agent model and judge model ids from environment/config; never print keys.
3. Runner safety:
   - Pass frozen DB path through agent state/retrieval metadata, not `CATALYST_DB_PATH`.
   - Keep `CATALYST_DB_PATH` pointing to trace/runtime DB or unset.
   - `TraceWriter` must write only to `--trace-db`.
   - Assert frozen DB is read-only and verify SHA before/after.
   - Use SQL fallback retrieval; no local LanceDB build, no sentence-transformers, no reranker.
4. Runner outputs:
   - `packages/eval/eval_cache/judge_cache.json`.
   - `packages/eval/eval_cache/refusal_scores.json`.
   - `packages/eval/eval_cache/s1_v1_baseline.json`.
   - Optional per-case evidence pack under `packages/eval/eval_cache/s1_v1_cases.json`.
5. Baseline metadata must include:
   - `frozen_db_sha256 = 0d97a7ec61b6ec8fb5f9263b0b37b0efc9755812739d7afead7f720c3567e8dd`.
   - `frozen_db_content_fingerprint`: total rows `13474`, `sum_length_content_md=66435087`, per-source counts.
   - `retrieval_mode = sql_fallback`.
   - `agent_model_id`, `judge_model_id`, `rubric_version`, `prompt_sha256`.
6. Run smoke first:
   - `.venv/bin/python packages/eval/scripts/run_s1_baseline.py --limit 5`
7. Only after smoke confirms no embedding/reranker load, run full 65.
8. Commit baseline artifacts as C5 after review.

### Phase C: S4 Core Gate before S2

1. Implement only the deterministic gate portion of S4 first.
2. Gate reads committed artifacts only:
   - `packages/eval/eval_cache/judge_cache.json`
   - `packages/eval/eval_cache/refusal_scores.json`
   - `packages/eval/eval_cache/s1_v1_baseline.json`
3. Gate must call zero live LLM.
4. Gate consumes answerable plus unanswerable cases.
5. Gate must fail when:
   - cache miss,
   - corrupted baseline,
   - changed `rubric_version`,
   - changed `judge_model_id`,
   - changed `retrieval_mode`,
   - metric falls below baseline margin,
   - baseline schema missing frozen SHA/content fingerprint.
6. Verification:
   - Run gate twice and check byte-stable outputs.
   - Temporarily corrupt a copied baseline fixture and prove red.
   - Network-disabled run should still pass.

### Phase D: Execute S2 under the S4 gate

1. Execute S2 plan after S4 Core is green.
2. Required tests to add or verify:
   - A state/config lacking `max_expansions` cannot silently trigger macro expansion.
   - `PolicyConfig.v1()` explicitly contains `temporal_penalty=0.9` and `max_expansions=0`.
   - Unknown pricing raises where judge/eval require cost accounting.
   - Agent path can carry `cost_status="unknown"` without crashing only where BYOK later allows it.
   - Unanswerable/refusal replay remains no-drift.
3. After each S2 subtask:
   - Run targeted tests.
   - Run S4 Core gate.
   - Check frozen DB SHA unchanged.

### Phase E: S4 Recalibration

1. After S2 lands, run measured config flips.
2. Produce delta tables for A1/A5/tier changes.
3. Keep `M=0.6` unless the user explicitly signs off `0.45`.
4. Produce ADR-012.
5. Do not overwrite C5 baseline; version any recalibration output separately.

### Phase F: BYOK and connectivity

1. Execute BYOK after S2 is merged and C5 baseline exists.
2. BYOK requirements:
   - Prompt-byte invariance: S1 cache survives.
   - Hosted rejects `server_env` with 403 at the resolver choke point.
   - Unknown cost means `cost_status="unknown"`, `cost_usd=None`; eval/judge raises when exact cost is required.
   - Eval never falls back across providers.
   - Task 4 must reuse S2 Phase 6, not rebuild it differently.
3. Connectivity amendment target:
   - `docs/plans/2026-07-08-byok-connectivity-amendment.md`.
4. Connectivity CORE:
   - Staged probe: env key, DNS, TCP, TLS, HTTP, auth, models, quota.
   - Deployment profiles including `demo-cn-gpu`.
   - Explicit `httpx.Client`/proxy injection in app provider clients when needed.
   - ProviderDoctor startup report and `GET /health/providers`.
   - Probe-before-save UX.
5. Connectivity deferred:
   - runtime multi-provider fallback beyond simple provider chain,
   - multi-server doctor,
   - S5/GPU hybrid baseline.

### Phase G: S5 and S6 later

- S5 GPU embed is not for the 8GB Mac.
- Run on GPU box/cloud after S2/S4/BYOK are stable.
- S5 will produce the hybrid live baseline; it must not be compared against SQL-fallback C5 baseline without explicit `retrieval_mode` versioning.

---

## 2. Commit Boundary Design

### C2: S1 ruler and S1 cleanup

Include only:

- `conftest.py`
- `packages/eval/**`
- `packages/agents/catalyst_agents/nodes/**`
- `packages/agents/catalyst_agents/state.py`
- `packages/agents/catalyst_agents/trace/**`
- `packages/agents/tests/test_adapter.py`
- `scripts/p1_trace_report.py`
- Any S1 cleanup of deleted metric imports/tests.

Do not include S3, docs-only C3, provider probes, or DB files.

### C3: docs and research

Include only:

- `docs/plans/**`
- `docs/research/**`

### C4: S3 data-belt remediation

Include only:

- `packages/data-core/catalyst_data/storage/sqlite.py`
- `packages/data-core/catalyst_data/migrations.py`
- `packages/data-core/catalyst_data/run_report.py`
- `packages/data-core/tests/test_s3_corpus_items.py`
- `packages/data-core/tests/test_s3_frozen_db_readonly.py`
- `packages/data-core/tests/test_s3_timestamp_canonical.py`
- `packages/data-core/tests/test_s3_watermark.py`
- `packages/data-core/tests/test_migrations.py`
- `packages/agents/catalyst_agents/retrieval/policy.py`

Do not include `data/catalyst_eval_frozen_v2.db`, `data/catalyst_eval_frozen_v2.db.damaged-0d97-backup`, or provider artifacts.

### C5: S1 baseline artifacts

Include only reviewed baseline artifacts under:

- `packages/eval/eval_cache/judge_cache.json`
- `packages/eval/eval_cache/refusal_scores.json`
- `packages/eval/eval_cache/s1_v1_baseline.json`
- optional `packages/eval/eval_cache/s1_v1_cases.json`

---

## 3. Known Problems and Solutions

| Problem | Impact | Solution |
|---|---|---|
| Frozen DB SHA changed | Old byte-invariant broken | Use current `0d97...` with content fingerprint in baseline metadata; keep file `chmod 444`; do not treat it as dev data. |
| C2 cannot collect tests | S1 cannot be committed honestly | Archive/port legacy deleted-metric scripts/tests before C2. |
| No S1 baseline runner | S4 has no real baseline | Build `packages/eval/scripts/run_s1_baseline.py` minimal SQL-only runner. |
| 8GB Mac cannot run S5 | Local embedding build may OOM | S1 baseline must be API-only SQL fallback; defer S5 to GPU box. |
| TraceWriter can write default DB | Could mutate frozen DB if env mis-set | Baseline runner uses separate `--trace-db`; never set `CATALYST_DB_PATH` to frozen DB. |
| S4 before baseline is meaningless | Gate cannot compare | Commit C5 baseline artifacts before S4 Core. |
| S2 before S4 risks unmeasured drift | Historical false-green pattern | Build S4 Core before S2; recalibrate after S2. |
| BYOK overlaps S2 | Conflicting model/cost edits | Run BYOK after S2; ensure Task 4 reuses S2 Phase 6. |

---

## 4. Verification Commands

### Frozen DB seal

```bash
shasum -a 256 data/catalyst_eval_frozen_v2.db
sqlite3 "file:data/catalyst_eval_frozen_v2.db?mode=ro" "PRAGMA integrity_check; PRAGMA quick_check;"
sqlite3 "file:data/catalyst_eval_frozen_v2.db?mode=ro" "SELECT source_type, COUNT(*) FROM clean_assets GROUP BY source_type ORDER BY source_type;"
sqlite3 "file:data/catalyst_eval_frozen_v2.db?mode=ro" "SELECT COUNT(*), SUM(LENGTH(content_md)) FROM clean_assets;"
```

Expected current values:

- SHA: `0d97a7ec61b6ec8fb5f9263b0b37b0efc9755812739d7afead7f720c3567e8dd`
- integrity: `ok`
- quick: `ok`
- total and content length: `13474 | 66435087`

### S1 cleanup

```bash
.venv/bin/python -m pytest packages/eval -q
.venv/bin/python -m pytest packages/agents -q
```

### S3

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_s3_corpus_items.py packages/data-core/tests/test_s3_frozen_db_readonly.py packages/data-core/tests/test_s3_timestamp_canonical.py packages/data-core/tests/test_s3_watermark.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_migrations.py -q
sqlite3 data/catalyst_dev_ws4b.db "SELECT CASE WHEN sql LIKE '%a.title || char(10) || COALESCE(a.description, '''') AS content_md%' THEN 'HAS_NEW_EXPR' ELSE 'MISSING_NEW_EXPR' END FROM sqlite_master WHERE type='view' AND name='corpus_items';"
```

### S1 baseline

```bash
.venv/bin/python packages/eval/scripts/run_s1_baseline.py --limit 5
.venv/bin/python packages/eval/scripts/run_s1_baseline.py
```

### S4 Core

```bash
.venv/bin/python -m pytest packages/eval/tests/test_s4_regression_gate.py -q
```

### S2

```bash
.venv/bin/python -m pytest packages/agents -q
.venv/bin/python -m pytest packages/eval -q
.venv/bin/python -m pytest packages/eval/tests/test_s4_regression_gate.py -q
```

---

## 5. Things Not To Do

- Do not set `CATALYST_DB_PATH` to `data/catalyst_eval_frozen_v2.db`.
- Do not run local embedding builds, BGE-M3, rerankers, or LanceDB rebuilds on the 8GB Mac.
- Do not commit `.db` files, damaged DB backups, provider discovery data, provider probe data, or live probe scripts.
- Do not create missing legacy R1 scripts just to satisfy orphan tests unless the user explicitly wants those old workflows restored.
- Do not run the full 65-case baseline before the 5-case smoke passes.
- Do not compare SQL-fallback baseline against future hybrid/S5 baseline without checking `retrieval_mode`.
- Do not change judge model, prompt, or rubric after C5 without creating a new baseline version.
- Do not let BYOK fallback across providers in eval/judge paths.

---

## 6. Review of Fable 5 Design

Fable's design is technically feasible and mostly correct. Adopt these parts:

- S4 Core before S2, S4 recalibration after S2.
- S1 baseline before S4/S2/BYOK.
- SQL-only/API-only baseline on the 8GB Mac.
- Separate trace DB from frozen retrieval DB.
- Re-pin current frozen SHA through baseline metadata rather than a heavy incident document.
- BYOK after S2 and C5 baseline artifacts.
- Connectivity amendment as an additive plan, not a blocker for baseline.

Adjust these parts:

- Keep the existing commit taxonomy: C2 is S1, C3 is docs/research, C4 is S3. Do not rename C3 to S3.
- Do not assume all legacy scripts must be permanently deleted; choose archive vs port based on grep usage and user intent.
- Treat `packages/data-core` full-suite async/plugin failures as a separate environment/test-dependency issue; do not bundle that into S1/S3/S2 unless it blocks targeted verification.
