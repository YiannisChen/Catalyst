# Pre-B6 Execution Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Execute the binding Pre-B6 convergence design to produce a promoted v13 snapshot, 40/40 probes, and production source bundle.

**Architecture:** Use existing production CLI (`catalyst_data.b2o`) + SEC live connector pipeline. All offline code already exists (195 tests green).

**Binding design:** `docs/plans/2026-07-29-pre-b6-evidence-convergence-design.md`
**Implementation reference:** `docs/plans/2026-07-29-pre-b6-evidence-convergence.md`

**Tech Stack:** Python 3.12, SQLite, existing B2 pipeline, SEC connectors

---

### Task 0: Preflight + Baseline Freeze

**Steps:**
1. Record branch, HEAD, git status
2. Verify baseline DB SHA = `7cc49ba1fd5245a6b8700ba7c26638d54f1dbb644f0aeb7cbaff684aa349f97e`
3. Run `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, `PRAGMA user_version` on baseline (read-only)
4. Check disk space
5. Check SEC_USER_AGENT is SET (not print value)
6. Run full Pre-B6 test suite (195 tests)
7. Record legacy fixture gaps

**Output:** `data/run_reports/b2e_baseline_freeze.json`

---

### Task 1: Bootstrap Candidate v13

**Steps:**
1. Build candidate path: `data/candidates/catalyst_b2e_v13_working.db`
2. Run CLI: `python -m catalyst_data.b2o bootstrap --source-db ... --expected-source-sha256 ... --bootstrap-db ...`
3. Verify candidate integrity, FK, user_version=13
4. Verify baseline SHA unchanged

---

### Task 2: S1 SEC Submissions (40 tickers)

**Steps:**
1. Build S1 plan with 40 `sec_submissions` cells using CLI
2. Execute S1 plan against candidate (parent_run_id=NULL)
3. Resume if interrupted
4. Verify 40/40 terminal-complete

---

### Task 3: S2 SEC Filing Index

**Steps:**
1. After S1 terminal, build S2 plan with `sec_filing_index` cells using CLI
2. Execute S2 plan (parent_run_id=NULL)
3. Verify all cells terminal-complete

---

### Task 4: S3 Freeze FilingInventoryManifest

**Steps:**
1. Build inventory from S1+S2 results
2. Write `data/manifests/sec_filing_inventory_<inventory_id>.json`
3. Record inventory_id
4. Baseline SHA unchanged

---

### Task 5: S4 SEC Document Fetch

**Steps:**
1. Load frozen inventory
2. Build S4 document plan (parent_run_id=NULL)
3. Execute S4 plan - download all mandatory document bodies
4. Resume if interrupted
5. Verify sec_source_ready=true

---

### Task 6: FMP Checkpoint Reconciliation

**Steps:**
1. Plan reconciliation for balance_sheet cells
2. Apply reconciliation (dry_run then real)

---

### Task 7: Pre-B6 Audit (sec_source_ready)

**Steps:**
1. Run `pre-b6-audit` CLI
2. Verify sec_source_ready=true

---

### Task 8: Pre-B6 Snapshot

**Steps:**
1. Run `pre-b6-snapshot` CLI
2. Get new snapshot_id ≠ baseline

---

### Task 9: Publish Corpus (filing_v3 + FTS)

**Steps:**
1. Run `pre-b6-publish-corpus` through the streaming publisher only
2. Resume the same build_id after a typed resource stop or interruption
3. Verify bounded source/chunk batches, normalized inventory identity, SQL
   reconciliation, and <=500-row committed FTS transactions
4. Verify the short selector cutover binds matching corpus+lexical identities
5. Get build_id, corpus_manifest_id, lexical_manifest_id, document count, chunk
   count, and page/iterator access metadata (no corpus-sized result payload)

---

### Task 10: Pre-B6 Promote (includes probes)

**Steps:**
1. Run `pre-b6-promote` CLI with --probe-cutoff and --probe-output
2. This runs: readiness re-run → 40/40 corpus coverage probes → 40/40 lexical smoke probes → promote_candidate()
3. Verify active pointer, promoted SHA, baseline unchanged

---

### Task 11: Lexical Baseline Freeze

**Steps:**
1. Freeze 12-case lexical baseline from promoted DB
2. Save top-20, cutoff, look-ahead, manifest identities

---

### Task 12: Export Production Source Bundle

**Steps:**
1. Run `export_source_bundle()` against promoted DB
2. Verify checksums, chunk count, non-empty, no vectors
3. Verify bundle idempotent

---

### Task 13: Final Verification

**Steps:**
1. Verify all completion criteria
2. Generate final report

---

### Completion Criteria
- v13 promoted DB exists, active pointer correct
- baseline SHA unchanged
- S1/S2/S4 lineages complete
- frozen inventory exact 40
- sec_source_ready=true, sec_evidence_ready=true
- filing_v3 > 0, covers 40/40
- corpus/FTS bound to new snapshot
- 40/40 probes pass
- lexical baseline frozen
- source_bundle exported with checksum verification
- no GPU vectors, no secrets, no push/PR
