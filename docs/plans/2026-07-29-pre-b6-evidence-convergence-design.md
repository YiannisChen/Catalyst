# Pre-B6 Evidence Convergence Design

- Date: 2026-07-29
- Status: **binding** (final convergence amendment)
- Supersedes: prior drafts of this file that described two-phase SEC discovery,
  aggregate chunk-count readiness, `filing_v2`/`filing_v2.1` ambiguity, 320/32
  token windows, parent-to-old-B2-O resume options, fake production embed
  vectors, optional gpt_web, and repair flags that bypass protected DB paths
- Implementation: `2026-07-29-pre-b6-evidence-convergence.md`
- Amends: technical contracts §3 (v12 + **v13**), roadmap **B2-E**, B2-O SEC
  metadata-only scope, B6 source-bundle grain, B7 evaluation slots

## 0. Code-verified facts (receiving-code-review)

| Fact | Code evidence |
|---|---|
| SEC rate | `config.RATE_POLICIES["dev"]["sec"] = RatePolicy(0.2, 3, None)` → min_interval 0.2s, max_concurrent 3, comment “5/sec conservative” |
| SEC retry | `retry.RETRY_POLICIES["sec"]`: 429 base 5 max 30 retries 3 jitter false; 5xx base 10 max 60 retries 3; timeout base 10 max 40 retries 2 |
| Retry-After | `_compute_rule_delay` uses `retry_after` then `min(delay, rule.max_seconds)` |
| Lineage resume | `_validate_b2_resume_lineage` requires entire parent chain `plan_hash` and `expected_plan_hash` match |
| EX-99 bug | `sec_normalize.resolve_filing_documents` hardcodes `document_type: "exhibit_99_1"` for every exhibit |
| Submissions alone incomplete | same module fetches index-headers only for selected 8-Ks after submissions parse |
| Chunk windows | `news_v2`: `MAX_TOKENS=384`, `TARGET_TOKENS=320`, `MAX_OVERLAP=48`, `MAX_PREFIX_TOKENS=64` |
| Profile CHECK | v9 DDL + triggers allow only `news_v2`,`filing_v2` |
| Tokenizer | `TOKENIZER_MODEL_ID=BAAI/bge-m3`, `TOKENIZER_REVISION=5617a9f61b028005a4858fdac845db406aefb181` |
| Provenance enum | `entity_type IN ('article','filing','macro_observation','ohlcv','fundamental_snapshot')` |
| Frozen write guard | `storage.sqlite.FROZEN_PATHS` + `_assert_not_frozen` |
| B5 default model | `llm_factory.DEFAULT_MODEL = "gemini-2.5-flash-nothink"`; `AIHUBMIX_BASE_URL = "https://aihubmix.com/v1"` |
| Temperature | not frozen in agents package today → freeze here for answer experiment |

## 1. Non-negotiable product requirements

1. All 40 ratified tickers support evidence-bounded attribution.
2. Evidence includes OHLCV, news, **SEC filing body text**, macro, usable fundamentals.
3. Cutoff-safe, citable, abstention-capable, zero look-ahead.
4. Mac: ingest/normalize/chunk/FTS; GPU: embed/rerank only.
5. Real-question experiments after B6; ticker count ≠ accuracy.
6. No overstated completion claims.

## 2. Baseline identity (immutable reference)

| Field | Value |
|---|---|
| Branch | `recovery/b2o-data-readiness` |
| HEAD (plan freeze epoch) | `9733cf870675001d23f15e40b2f1cf229a6672cf` |
| Baseline snapshot_id | `d5e5f7fef11581c1f516bc484f64e31c1776a4202eb0c68aeada0bd0b98817c0` |
| Baseline DB sha256 | `7cc49ba1fd5245a6b8700ba7c26638d54f1dbb644f0aeb7cbaff684aa349f97e` |
| Baseline corpus_manifest_id | `29010eef2bcf00258293463ab3d6cb77ef07a07dc2a48bf04b7b4ecdfd1e8a33` |
| Baseline terminal_run_id | `b2-e44bb9625f594d8f970150f884201a95` |
| Baseline plan_hash | `c335b918c3aef939e3deab922233fd120e0d270373d6353264fb424cb5d9866f` |

Post-convergence promotion **never overwrites** the baseline snapshot file.

## 3. Phase topology

```text
B2-E-I  offline: v13, SEC inventory/index/document pure modules, readiness,
        checkpoint reconciliation, filing_v3, source bundle, probes/cases
B2-E-X  authorized live: S1 root plan then S2 root plan; S3 freeze offline
B2-E-D  authorized live: S4 document root plan (parent_run_id=NULL; not parented to S1/S2)
B2-E-R  offline on candidate: plan/apply request_count reconciliation
B2-E-S  sec_source_ready → DataSnapshotManifest → filing_v3+FTS →
        sec_evidence_ready → probes → PROMOTE → promote_candidate()
B4-L-B  freeze lexical baseline artifacts
B6-L    explicit dense/RRF/hybrid/rerank/source_bundle/import fixtures (no GPU)
B6-G    GPU only after Pre-B6 GO
B6-E    multi-arm retrieval + B5 answer experiment + gpt controls
```


---

## A. SEC four logical stages (discovery then fetch)

### A.1 Stage S1 — Submissions inventory

| Field | Value |
|---|---|
| source_type | `sec_filings` |
| endpoint_name | `sec_submissions` |
| subject | ticker (40) |
| window | as_of `canonical_end` |
| HTTP | `GET submissions/CIK##########.json` |

**Produces:** filing metadata rows; submissions `raw_assets`; checkpoint terminal.
**Does not produce:** body-ready documents; complete EX-99 enumeration.

**Binding fact:** submissions JSON is **insufficient** to fully enumerate EX-99
filenames and types. Stage S2 is mandatory for every selected filing.

```text
submissions_cell_id = SHA256(canonical_json({
  "source_type": "sec_filings",
  "endpoint_name": "sec_submissions",
  "ticker_or_series": ticker,
  "window_start": "2026-07-23",
  "window_end": "2026-07-23",
  "stage": "evidence",
  "provider_profile_version": "v1"
}))
```

### A.2 Stage S2 — Filing index discovery

For every filing selected by form policy (canonical window or carry-in), emit
exactly one index cell:

| Field | Value |
|---|---|
| source_type | `sec_filings` |
| endpoint_name | `sec_filing_index` |
| subject | ticker |
| window_start = window_end | `filed_date` (YYYY-MM-DD) |

```text
index_cell_id = SHA256(canonical_json({
  "source_type": "sec_filings",
  "endpoint_name": "sec_filing_index",
  "ticker_or_series": ticker,
  "window_start": filed_date,
  "window_end": filed_date,
  "stage": "evidence",
  "provider_profile_version": "v1",
  "form_policy_version": "v1",
  "accession_number": accession_number,
  "cik": cik_zero_padded_10
}))
```

**HTTP request / fallback order (binding):**

```text
base = Archives/edgar/data/{cik_int}/{accession_nodash}/
1. GET {base}{accession}-index.html          # canonical filing index
2. if 404 or document table unparseable:
   GET {base}{accession}-index-headers.html  # headers HTML
3. if still fail (legacy path only):
   GET {base}{accession}-index-headers.htm   # historical .htm
```

Each real HTTP request writes `provider_request_attempts` + `raw_assets`.
**Success:** any candidate returns HTTP 200 and parses a document table that
includes a primary document.
**Failed:** all candidates fail, no primary, or descriptor identity conflict.
**Parse into ordered document descriptors:** primary flag, filename, SEC sequence
(integer if present), document type, description, content extension, normalized URL,
**`requiredness`**, **`requiredness_reason`** (frozen at S3; see §B).

**Forbidden:** discovering or appending documents during Stage S4.

### A.3 Stage S3 — Frozen FilingInventoryManifest

Runtime path (gitignored):

```text
data/manifests/sec_filing_inventory_<inventory_id>.json
```

`.gitignore` entry required:

```text
/data/manifests/sec_filing_inventory_*.json
```

```text
inventory_id = SHA256(canonical_json({
  "schema_version": "1.0.0",
  "source_snapshot_id": source_snapshot_id,
  "universe_manifest_id": universe_manifest_id,
  "canonical_start": "2025-08-01",
  "canonical_end": "2026-07-23",
  "carry_in_rule_version": "v1",
  "form_policy_version": "v1",
  "document_selection_policy_version": "v1",
  "sorted_filing_entries": [ ... ]
}))
```

**Forbidden in inventory_id:** document `plan_hash`, run_id, executor versions,
or any field that creates circular identity with document planning.

Each filing entry contains:

- ticker, cik, accession_number, form_type, filed_at (`…Z`)
- primary_document filename
- submissions_raw_sha256
- filing_index_raw_sha256
- in_canonical_window, is_carry_in
- `documents`: complete, deterministically sorted descriptors

**Document sort order (binding):**

1. Primary document first.
2. Remaining documents by SEC sequence ascending (numeric).
3. If sequence missing: sort by `(document_type, filename, document_url)`.

**EX-99 roles** after sort, assigned in order among EX-99* types:

```text
exhibit_99_1, exhibit_99_2, exhibit_99_3, ...
```

Never assign every exhibit `exhibit_99_1` (current `sec_normalize.py` bug).

Each document descriptor at freeze time includes:

| Field | Binding |
|---|---|
| requiredness | `mandatory` \| `optional_degraded` |
| requiredness_reason | e.g. `primary`, `ex99_html`, `ex99_pdf`, `ex99_unknown_ext` |

`requiredness` enters the descriptor objects hashed into `inventory_id`.
**S4 responses must not change the mandatory denominator.**

### A.4 Stage S4 — Document fetch (new root run)

```text
document_cell_id = SHA256(canonical_json({
  "source_type": "sec_filings",
  "endpoint_name": "sec_document",
  "ticker_or_series": ticker,
  "window_start": filed_date,
  "window_end": filed_date,
  "stage": "evidence",
  "provider_profile_version": "v1",
  "inventory_id": inventory_id,
  "accession_number": accession_number,
  "document_role": document_role,
  "document_file": document_file,
  "document_url": canonical_document_url
}))
```

```text
document_id = SHA256(canonical_json({
  "filing_id": filing_id,
  "accession_number": accession_number,
  "document_role": document_role,
  "document_file": document_file,
  "document_url": canonical_document_url
}))
```

**Document plan_hash includes:**

- `inventory_id`
- ordered list of all document_cell_id values
- `executor_policy_version` = `"v1"`
- `retry_policy_version` = `"sec_v1"` (maps to production SEC RetryPolicy)
- `rate_policy_version` = `"sec_dev_v1"` (maps to RatePolicy 0.2/3/None)

**Document fetch run:** first run `parent_run_id = NULL` (new root plan family).

### A.5 Three independent root plan families (binding)

**Binding phrase:** three independent root plan families = S1, S2, S4.

| Family | Cells | First run | Resume | May parent? |
|---|---|---|---|---|
| **S1** submissions | exactly 40 static `sec_submissions` cells | `parent_run_id=NULL` | only same S1 `plan_hash` lineage | never S2/S4 |
| **S2** index | ordered `sec_filing_index` cells after S1 terminal | `parent_run_id=NULL` | only same S2 `plan_hash` lineage | never S1/S4 |
| **S4** document | ordered `sec_document` cells after S3 freeze | `parent_run_id=NULL` | only same S4 document `plan_hash` lineage | never S1/S2 |

S2 `plan_hash` includes: ordered `index_cell_id` list +
`form_policy_version` + `carry_in_rule_version` + `rate_policy_version` +
`retry_policy_version`.

S1, S2, S4 **must not** use each other as `parent_run_id`.
Cross-stage correlation: `source_snapshot_id`, raw SHAs, `inventory_id`, artifact
hashes only. Never parent onto old B2-O runs (`_validate_b2_resume_lineage`
requires full-chain plan_hash equality).

### A.6 SEC plan-cell record v2 (full identity without legacy drift)

The existing `manifests.universe.SourceCell` schema hashes only the shared B2-O
fields and is retained unchanged so historical B2-O cell IDs and plan hashes do
not drift. New S2/S4 cells use a frozen **SEC plan-cell record v2**, represented
as a plain canonical mapping accepted by the existing plan/executor record path.

```text
sec_plan_cell_v2 = {
  # existing shared fields, unchanged
  stage, source_type, endpoint_name, subject,
  window_start, window_end, date_domain,
  provider_profile_version, page_cap, item_cap,

  # versioned SEC extension
  identity_schema_version: "sec_cell_v2",
  identity_extensions: {
    # S2: cik, accession_number, form_policy_version
    # S4: inventory_id, accession_number, document_id,
    #     document_role, document_file, document_url,
    #     requiredness, requiredness_reason
  },
  cell_id
}

cell_id = SHA256(canonical_json(all fields above except cell_id))
```

Bindings:

1. Empty extensions are forbidden for `sec_filing_index` and `sec_document`.
2. Extension keys are exact and endpoint-specific; unknown or missing keys fail
   plan validation.
3. The full record enters `UpdatePlan.stages[*].cells`, so the plan hash binds
   the extension values.
4. Transport receives the full record, not only ticker/date/source.
5. `provider_request_attempts.request_params_redacted` records the non-secret
   extension identity; `request_fingerprint` binds the canonical request URL.
6. `source_checkpoints` continues to persist `cell_id`, endpoint, window, and
   provider profile. Resume resolves full identity from the frozen plan and
   matches by `(run lineage, cell_id)`; it never reconstructs identity from the
   reduced checkpoint columns.
7. Legacy `SourceCell.create(...)` with no SEC extension must produce byte-for-
   byte identical identities and cell IDs to the v12 implementation.

---

## B. Form and document selection policy

| Constant | Value |
|---|---|
| `canonical_start` | `2025-08-01` |
| `canonical_end` | `2026-07-23` |
| `carry_in_floor` | `2024-01-01` |
| `carry_in_rule_version` | `v1` |
| `form_policy_version` | `v1` |
| `document_selection_policy_version` | `v1` |

**US domestic mandatory classes:**

- Event: `8-K`, `8-K/A`
- Periodic class 10-Q: `10-Q`, `10-Q/A`
- Periodic class 10-K: `10-K`, `10-K/A`

**Foreign private issuer mandatory classes:**

- Event: `6-K`
- Periodic class 20-F: `20-F`, `20-F/A`

**Periodic carry-in v1:** if a mandatory periodic class has zero filings with
`filed_date ∈ [canonical_start, canonical_end]`, select the single most recent
filing of that class with `carry_in_floor ≤ filed_date < canonical_start`. If
none exists → **missing mandatory slot** → blocks `sec_source_ready`.

**Event filings:** no carry-in.

**Requiredness frozen at S3 (descriptor extension at inventory freeze):**

| Descriptor rule | requiredness | requiredness_reason |
|---|---|---|
| every primary document | `mandatory` | `primary` |
| EX-99 with ext `.html`/`.htm`/`.txt` | `mandatory` | `ex99_html` / `ex99_htm` / `ex99_txt` |
| EX-99 with ext `.pdf` | `optional_degraded` | `ex99_pdf` |
| EX-99 unknown or other non-PDF extension | `mandatory` | `ex99_unknown_ext` |

**S4 response must not change denominator:**

| Response event | Effect |
|---|---|
| mandatory descriptor body is PDF (Content-Type/magic) | `mandatory_failed` — **never** downgrade to optional |
| optional PDF EX-99 | no OCR; report only; not in mandatory set |
| primary PDF-only | always `mandatory_failed` |
| empty PDF extract | never `success` |

Report counts: `pdf_only_primary_count`, `pdf_only_exhibit_count`,
`optional_degraded_ids`.

---

## C. Transport, retry, materialization

### C.1 RatePolicy (production `config.py`)

```text
provider = sec
min_interval_sec = 0.2
max_concurrent = 3
effective_ceiling ≈ 5 requests/sec   # 1/0.2, concurrent ≤ 3
daily_budget = None
cooperative cancellation = before each document cell and between pages
```

### C.2 RetryPolicy (production `retry.py` SEC entry)

| Class | base_s | max_s | max_retries | jitter |
|---|---:|---:|---:|---|
| 429 / rate_limit | 5 | 30 | 3 | false |
| 5xx | 10 | 60 | 3 | false |
| timeout / transport | 10 | 40 | 2 | false |

**Retry-After binding:**
`delay = min(max(retry_after, contractual_exponential_delay), rule.max_seconds)`
Retry-After must not **shorten** below contractual exponential delay; max cap still applies.

Every attempt → `provider_request_attempts`.
`checkpoint.request_count == COUNT(attempts WHERE logical_fetch_id = …)`.

### C.3 Success materialization

For each successful document fetch:

1. `raw_assets` row (response bytes + sha256)
2. `provider_request_attempts` row(s)
3. `filing_documents` row with non-empty extract when success
4. `normalized_provenance` with:

```text
entity_type = "filing"          # existing CHECK; no enum expansion
entity_id = document_id
entity_version = SHA256(canonical_json({
  "document_id": document_id,
  "response_sha256": response_sha256,
  "extracted_text_sha256": extracted_text_sha256,
  "extraction_normalizer_version": "sec_extract_v1"
}))
```

**Forbidden evidence of body readiness:** empty text, placeholder
`filing_documents`, or submissions/index success alone.

**Text quality gate for success:**

```text
len(normalized_text.strip()) >= RAG_MIN_CHAR_COUNT   # config default 200
AND tokenizer count_tokens(normalized_text) >= 1
```

---

## D. Source readiness vs evidence readiness (split)

**Delete:** any aggregate condition
`filing_chunk_count >= mandatory_success_document_count`.

```text
mandatory_document_ids =
  exact set of document_id values in frozen FilingInventoryManifest
  with requiredness = 'mandatory' (frozen at S3; S4 cannot shrink/grow set)
```

### D.1 `sec_source_ready` (source tables only; **no** corpus_chunks)

```text
sec_source_ready ⇔
  (1) 40/40 sec_submissions terminal-complete (S1 lineage)
  ∧ (2) every selected sec_filing_index cell terminal-complete (S2 lineage)
  ∧ (3) every mandatory periodic class slot filled (in-window or carry-in)
  ∧ (4) ∀ document_id ∈ mandatory_document_ids:
          checkpoint.status = 'success'
        ∧ is_complete = 1
        ∧ extraction_status = 'success'
        ∧ quality gate (C.3)
        ∧ request_count = attempt_count ≥ 1
        ∧ raw/provenance identity complete
  ∧ (5) no mandatory missing / failed / partial / unresolved
  ∧ (6) optional_degraded IDs reported only; not in mandatory_document_ids
  # does NOT inspect corpus_chunks
```

**DataSnapshotManifest** `coverage_states` binds **`sec_source_ready`** (and overall
mandatory gates that depend on source completeness). It does **not** require
`sec_evidence_ready` or any derived corpus chunk count.

### D.2 `sec_evidence_ready` (requires filing_v3 corpus)

```text
sec_evidence_ready ⇔
  sec_source_ready = true
  ∧ ∀ document_id ∈ mandatory_document_ids:
      COUNT(corpus_chunks WHERE
        document_id = :id
        AND chunk_profile_version = 'filing_v3'
        AND status IN ('active','pending_embedding','embedded','metadata_only')
      ) >= 1
```

### D.3 Report fields

```text
expected_mandatory_count
fetched_count
extracted_count
provenance_valid_count
chunked_count
missing_document_ids
failed_document_ids
optional_degraded_ids
missing_carry_in_slots
pdf_only_primary_count
pdf_only_exhibit_count
sec_source_ready
sec_evidence_ready
```

### D.4 Required tests

| Test | Expected |
|---|---|
| source complete, zero corpus_chunks | `sec_source_ready=true`, `sec_evidence_ready=false` |
| DataSnapshotManifest build when source ready, no corpus | allowed (uses sec_source_ready) |
| document A has 10 filing_v3 chunks, B has 0 | `sec_evidence_ready=false`; B in missing/failed chunk set |
| both A and B ≥1 filing_v3 searchable chunk | `sec_evidence_ready=true` |

---

## E. filing_v3 and migration v13

### E.1 Profile

```text
chunk_profile_version = "filing_v3"
```

Only profile used for new SEC body chunks after B2-E. Existing `filing_v2` rows
remain readable; not rewritten in place.

### E.2 Migration v13 (always table rebuild)

Owner: data-core. Applied on **candidate** only.
SQLite cannot ALTER CHECK in place — **v13 always rebuilds** `corpus_chunks`.

**Exact algorithm:**

```text
1. SAVEPOINT migration_v13
2. CREATE TABLE corpus_chunks_v13 (
     … exact v9 column list, FKs, UNIQUE(document_id, chunk_profile_version, section_key, ordinal),
     chunk_profile_version CHECK IN ('news_v2','filing_v2','filing_v3')
   )
3. INSERT INTO corpus_chunks_v13 SELECT … FROM corpus_chunks  (all columns)
4. validate: COUNT(*) match and deterministic full-row logical hashes match
5. DROP TABLE corpus_chunks
6. ALTER TABLE corpus_chunks_v13 RENAME TO corpus_chunks
7. recreate every index and trigger listed below
8. PRAGMA foreign_key_check  (must be empty)
9. PRAGMA user_version = 13
10. RELEASE SAVEPOINT; on any failure: ROLLBACK TO migration_v13; RELEASE; re-raise
```

**Indexes to recreate (exact names from v9):**

- `idx_corpus_chunks_document`
- `idx_corpus_chunks_available`
- `idx_corpus_chunks_source_class`
- `idx_corpus_chunks_manifest`

**Triggers to recreate (exact names; profile list includes filing_v3):**

- `trg_corpus_chunks_insert_guard`
- `trg_corpus_chunks_update_guard`

(Do not invent additional index/trigger names. If `idx_index_state_chunk_id`
exists from prior publish, leave it — it is not part of v9 corpus_chunks DDL.)

**`filing_documents.document_id` additive identity in the same v13 migration:**

1. `ALTER TABLE filing_documents ADD COLUMN document_id TEXT` only when absent.
2. Create `idx_filing_documents_document_id` as the unique partial index in E.5.
3. Create `trg_filing_documents_document_id_insert_guard`.
4. Create `trg_filing_documents_document_id_update_guard`.
5. Legacy NULL values remain valid; new S4 materialization requires non-null.

**Preserve:** all existing rows including `filing_v2`; never rewrite embedded
filing_v2 identities. New SEC body uses filing_v3; reconciliation may tombstone
with `profile_version_replaced`.

Registry: `Migration(version=13, name="filing_v3_profile", reversible=False)`.
`CURRENT_SCHEMA_VERSION = 13` after implementation.

Tests: fresh→13; 12→13 preserves filing_v2; injected failure restores v12;
trigger rejects unknown profile; accepts filing_v3.

### E.3 Unified chunk contract (binding; matches production news_v2 constants)

| Parameter | Value |
|---|---:|
| maximum total tokens | 384 |
| nominal target tokens | 320 |
| maximum body overlap | 48 |
| maximum prefix tokens | 64 |
| tokenizer | BAAI/bge-m3 @ `5617a9f61b028005a4858fdac845db406aefb181` |

**Forbidden:** 320/32 conflict pair; whole-filing single-chunk fallback for
documents that exceed one target window.

### E.4 Section policy by form

| Form | Section rules |
|---|---|
| 8-K / 8-K/A | Item x.xx headings |
| EX-99 roles | each document_role is its own section root |
| 6-K | role-bounded primary section, then semantic windows |
| 10-Q / 10-Q/A | Part I/II + Item headings |
| 10-K / 10-K/A | Part I–IV + Item headings |
| 20-F / 20-F/A | Item 1–19 headings |
| parse failure | `section_key=unknown_000`, `section_parse_degraded=1`, bounded windows only |

`available_at` for all new chunks: UTC second-resolution ending in `Z`.

### E.5 Persisted document identity

Migration v13 also adds nullable `filing_documents.document_id TEXT` for legacy
compatibility. New S4 rows require the 64-character lowercase-hex `document_id`
from A.4.

```text
CREATE UNIQUE INDEX idx_filing_documents_document_id
ON filing_documents(document_id)
WHERE document_id IS NOT NULL
```

The v13 insert/update guards enforce lowercase SHA-256 shape and make a non-null
`document_id` immutable. Existing pre-B2-E rows may remain NULL. Readiness,
`normalized_provenance.entity_id`, and `corpus_chunks.document_id` all use this
persisted value; no downstream module may substitute
`filing_id` or `filing_id:document_type`.

---

## F. Checkpoint reconciliation (request_count only)

**Do not** add incident-specific `fmp/lineage_repair.py`.
**Do not** auto-recompute `pages_received` / `items_received` in this wave.

**Module:** `packages/data-core/catalyst_data/ingestion/checkpoint_reconciliation.py`

```text
plan_checkpoint_reconciliation(...) -> immutable ProposedCheckpointChanges
apply_checkpoint_reconciliation(..., expected_plan_hash, dry_run=True)
```

| Rule | Binding |
|---|---|
| `dry_run=True` | zero writes |
| path guard | candidate realpath only; frozen / `data/snapshots/` / active promoted → refuse; **no bypass flag** |
| identity preflight | bind `run_id`, `cell_id`, `logical_fetch_id`, `endpoint_name` |
| allowed field change | `request_count = COUNT(provider_request_attempts WHERE logical_fetch_id=…)` only |
| eligibility | ≥1 attempt with status `SUCCEEDED` and matching `raw_asset` |
| never modify | `status`, `cell_id`, `raw_asset_id`, `pages_received`, `items_received` |
| apply transaction | `BEGIN IMMEDIATE`; verify `cursor.rowcount` exact; mismatch → full rollback |
| second apply | idempotent (no-op when already equal) |
| audit | atomic JSON under `data/run_reports/` with before/after field hashes |

After repairing the 25 balance_sheet lineage-broken cells: re-audit.
45× HTTP 402 remain optional degraded; **do not block** mandatory GO.

---

## G. Snapshot / corpus / FTS / promote order (binding)

**Forbidden orderings:**

- readiness-that-requires-chunks → corpus → DataSnapshotManifest
- corpus built before `snapshot_id`
- `sec_evidence_ready=true` required before filing_v3 corpus exists

### G.1 Composite convergence identity

Three independent SEC plan families mean no single ingestion run plan hash
identifies the converged snapshot. Before building `DataSnapshotManifest`,
compute:

```text
reconciliation_evidence_hash = SHA256(canonical_json({
  schema_version,
  sorted proposed/applied row identities,
  sorted before_request_counts,
  sorted after_request_counts
}))
# excludes created_at, report path, and other operational metadata

convergence_plan_hash = SHA256(canonical_json({
  "schema_version": "b2e_convergence_v1",
  "baseline_snapshot_id": baseline_snapshot_id,
  "universe_manifest_id": universe_manifest_id,
  "s1_plan_hash": s1_plan_hash,
  "s2_plan_hash": s2_plan_hash,
  "inventory_id": inventory_id,
  "s4_plan_hash": s4_plan_hash,
  "reconciliation_evidence_hash": reconciliation_evidence_hash,
  "db_user_version": 13,
  "readiness_policy_version": "b2e_readiness_v1"
}))
```

`DataSnapshotManifest.plan_hash` is exactly `convergence_plan_hash`. It is not
any individual S1/S2/S4 plan hash. Reordering or changing any bound component
changes the identity; timestamps and file paths do not.

**Required order:**

```text
 1  baseline SQLite backup → data/candidates/…working.db
 2  migration v13 (table rebuild)
 3  S1 submissions plan (root; 40 cells)
 4  S2 filing-index plan (root; after S1 terminal)
 5  freeze S3 FilingInventoryManifest (inventory_id; requiredness frozen)
 6  S4 document fetch plan (root; after S3)
 7  FMP plan_checkpoint_reconciliation + apply (candidate only)
 8  sec_source_ready MUST be true
 9  compute convergence_plan_hash; build DataSnapshotManifest → snapshot_id
    (coverage binds sec_source_ready; plan_hash=convergence_plan_hash)
10  build filing_v3 corpus with certified_snapshot_identity=snapshot_id
11  build FTS / lexical_index_state (one transaction)
12  sec_evidence_ready MUST be true
13  40 ticker corpus coverage invariant + lexical index smoke probes
14  integrity_check / FK / hash / identity verification
15  obtain PROMOTE authorization
16  call existing promote_candidate() ONCE
    (os.replace candidate→versioned snapshot + atomic active pointer)
17  verify active pointer, new snapshot SHA, baseline SHA unchanged
18  lexical baseline freeze (12 cases + probe artifacts)
19  production source_bundle export
```

**Promote API (production `manifests.operations.promote_candidate`):**
single call replaces candidate path with versioned snapshot **and** writes
active pointer. There is **no** separate publish-then-promote API this wave.
Failure must not leave a half-updated pointer. Baseline snapshot file never moves.

Any failure before step 16 → no `promote_candidate`.

---

## H. B6-L production source bundle (no GPU this wave)

### H.1 Forbidden

- Production export containing fake vectors
- Fake vectors only in unit-test fixtures

### H.2 Production source bundle layout

```text
data/source_bundles/source_<source_bundle_id>/
  source_bundle_manifest.json
  chunks.jsonl
  checksums.sha256
```

Gitignore: `/data/source_bundles/`

Each `chunks.jsonl` line (chunk_id sorted):

```text
chunk_id, document_id, content_text, content_hash, metadata_hash,
available_at, ticker_associations, corpus_manifest_id, chunk_profile_version
```

```text
source_bundle_id = SHA256(canonical_json({
  "schema_version": "1.0.0",
  "corpus_manifest_id": corpus_manifest_id,
  "snapshot_id": snapshot_id,
  "ordered_chunk_record_hashes": [SHA256(each_line_canonical), ...]
}))
```

Export rules: sort by chunk_id; exclude tombstones; only frozen
corpus_manifest_id; no API keys, raw payloads, request ledger, or whole SQLite DB;
verify counts and content hashes.

### H.3 GPU return contract (for later B6-G; design only here)

GPU returns float32 LE L2-normalized vectors, ordered chunk IDs, per-batch
checksums, model/revision/dim/dtype/normalization, `source_bundle_id`, IndexManifest.

Mac import verifies source_bundle_id, order, count, dim, all checksums.

### H.4 VRAM (not a static formula gate)

```text
peak_vram_bytes ≈ batch * seq * hidden * activation_bytes * safety
```

is a **rough planning estimate only**, not a safety gate.

Operational GPU rules (B6-G):

1. batch=1 measured preflight; record `torch.cuda.max_memory_allocated/reserved`
2. initial batch ceiling 32
3. OOM → halve batch, clear cache, resume incomplete batch
4. batch=1 OOM → hard fail
5. no silent CPU fallback

### H.5 Model pins

| Constant | Value |
|---|---|
| BGE_M3_REVISION | `5617a9f61b028005a4858fdac845db406aefb181` |
| BGE_RERANKER_REVISION | `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` |
| dim | 1024 |
| dtype | float32 LE |
| normalization | l2 |

`build_embeddings_gpu.py` = **LEGACY_FROZEN_EVAL** only (`clean_assets` frozen DB).

---

## I. Coverage probes and attribution experiment

### I.1 40/40 probes (two gates; not retrieval-quality proof)

**Forbidden:** documented-empty pass as success.

#### A. Corpus coverage invariant (per ticker)

All must hold against frozen `corpus_manifest_id` and probe cutoff:

1. ≥1 eligible chunk for that ticker
2. ≥1 chunk with `chunk_profile_version='filing_v3'` for that ticker
3. every inspected chunk belongs to frozen corpus_manifest_id
4. `available_at <= probe_cutoff` for those chunks

#### B. Lexical index smoke probe (per ticker)

**Binding name:** lexical index smoke probe.

Deterministic construction (not a quality metric):

1. Among eligible filing_v3 chunks for the ticker, pick **anchor** =
   first by `chunk_id` ascending sort.
2. Normalize `anchor.content_text` with Unicode NFC and lowercase; extract
   lexical terms using regex `[a-z0-9]+`; remove the exact
   `smoke_stopwords_v1` set:
   `{a, an, and, are, as, at, be, by, for, from, in, is, it, of, on, or,
   that, the, this, to, was, were, with}`.
3. Deduplicate while preserving encounter order. Take the first **8** remaining
   terms (at least one required) and emit an escaped FTS5 AND query. Do not use
   BGE subword tokens as FTS terms.
4. Retrieve with ticker filter = that ticker, cutoff = `anchor.available_at`,
   top_k=8.
5. Pass iff top-8 contains `anchor.chunk_id` **or** any sibling with same
   `document_id`.
6. Persist in probe artifact: query terms, anchor chunk_id, cutoff, hit mode.

This is **index/coverage smoke only**. Real retrieval quality is measured by
the 12 attribution cases (I.2+).

### I.2 Twelve mutually exclusive cases

| Slot | Count |
|---|---:|
| single-source answerable | 4 |
| multi-source answerable | 2 |
| correct-abstain | 2 |
| unsupported-cause / distractor | 2 |
| temporal look-ahead traps | 2 |
| **Total** | **12** |

### I.3 Retrieval arms (metrics pool)

`lexical`, `dense`, `hybrid`, `reranked`

Metrics on these arms only: Recall@8/20, nDCG@8, candidate equality, look-ahead.

### I.4 Answer experiment

Same B5 attribution workflow for each retrieval arm.
**Only variable:** retriever arm.

**Frozen model (code-verified default):**

| Param | Binding value |
|---|---|
| provider | `aihubmix` |
| base_url | `https://aihubmix.com/v1` |
| model_id | `gemini-2.5-flash-nothink` |
| temperature | `0.0` |
| max_tokens | `2048` |
| case order | fixed JSONL order |
| cutoff | per-case frozen |

If implementation cannot load this exact model_id via production
`build_llm` path, Task 16 status = **`BLOCKED_BY_MODEL_FREEZE`** (no silent substitute).

### I.5 gpt_same_evidence

Separate **reasoning control**, not a retrieval arm.
Reads frozen hybrid top-20 chunk texts only. Same model freeze as I.4.

### I.6 gpt_web (required external comparison)

Required human external benchmark; **does not block** reranker KEEP/KILL.
Log: exact web product/model label displayed, prompt, answer timestamp `Z`,
claimed cutoff, citations/URLs, possible look-ahead, human correctness judgment.
**Never** aggregate scores with internal arms.

### I.7 Reranker gate

```text
bootstrap resamples = 10000
bootstrap seed = 20260729
method = case-level bootstrap of Δ nDCG@8 (reranked − hybrid)
```

```text
KEEP ⇔
  CI_lower(Δ nDCG@8) > 0
  ∧ candidate_set_equality_rate == 1.0
  ∧ look_ahead_violations == 0
  ∧ abstention_correctness_reranked >= abstention_correctness_hybrid
else KILL
```

12 cases support **pilot claim only** — not general statistical significance.

### I.8 Historical baseline

If original 10-ticker system snapshot unrecoverable:
`baseline_mode = protocol_continuity` — no invented historical scores.

---

## J. Honest claim language

| Allowed | Forbidden |
|---|---|
| submissions complete; body pending | SEC evidence ready (pre-S4) |
| FMP optional: 402 degraded; lineage repaired | FMP 75/120 ready |
| searchable pending_embedding rows | active embedded rows (pre-B6-G) |
| pilot n=12 | general significance |

## K. Pre-B6 GO (GPU front door)

```text
GO ⇔
  baseline DB sha256 unchanged
  ∧ active DB user_version = 13
  ∧ sec_source_ready
  ∧ sec_evidence_ready
  ∧ fmp request_count broken count == 0
  ∧ new snapshot_id ≠ baseline
  ∧ new corpus_manifest_id ≠ baseline
  ∧ 40/40 corpus coverage invariants pass
  ∧ 40/40 lexical index smoke probes pass
  ∧ lexical baseline frozen for 12 cases
  ∧ source_bundle_id published (no vectors)
  ∧ B6-L module fixture suite green (design-linked task list)
  ∧ no production vectors yet
  ∧ promote_candidate already succeeded under PROMOTE auth
```

Otherwise **NO_GO**.
