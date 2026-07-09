# Hardening Phase H1 — Correctness & Operator Contract Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix five verified correctness defects in data-core (index_summary KeyError, dry-run writes, publisher misclassification, hard-coded PASS guards, missing doctor command) to establish a trustworthy operator contract for the indexing pipeline.

**Architecture:** Fixes are surgical, single-module changes to `index_builder.py`, `source_tier.py`, `cli_index.py`. A new `doctor.py` module composes four existing audit gates (audit_gate_p0, audit_embed_readiness) plus two new checks (unknown-publisher audit, dry-run zero-write assertion) into a single `doctor` CLI command with `--json` output and non-zero exit on failure. No new dependencies — httpx + stdlib only.

**Tech Stack:** Python 3.11, sqlite3 (PRAGMA query_only), pytest (TDD), existing audit infrastructure (coverage_audit.py gates).

**Branch:** `ws4b/article-level-data` (data-core only; never touch packages/app or packages/agents)

**DB:** `data/catalyst_dev_ws4b.db` (dev), frozen DB SHA `0dfc81b1…ecdcdf` (never change)

---

## Defect Inventory (5 items)

| # | Defect | Root Cause | Failure Mode |
|---|--------|-----------|--------------|
| D1 | `index_summary` KeyError: `'article_id'` | Filing records use `corpus_item_id`/`source_kind`; article records use `article_id` only. `index_summary` accesses `r["article_id"]` which filing records lack. | Crashes on any DB with filing data |
| D2 | `cmd_rebuild_index --mode dry-run` writes | Calls `classify_articles(conn)` which runs `UPDATE ... SET source_tier` + `conn.commit()` | Violates dry-run contract; mutates DB |
| D3 | "SeekingAlpha" → T4 instead of T5 | `tier_for_publisher` does exact match after `strip()` only; no canonicalization | Publisher misclassification |
| D4 | Hard-coded PASS strings in CLI output | `print("PASS ...")` with no computed check; also uses filing-inclusive l1_count for article-scoped guard | Silent false passes if guards change; false failure on DBs with filings |
| D5 | No doctor command | Fragmented audit invocation; no single `doctor --json` entrypoint | Operator must run 3+ commands manually |

---

## New CLI Contract

```
catalyst_data.cli_index status [--freshness] [--db PATH]
  ↳ Prints index/freshness summary. Read-only. Exit 0 always.

catalyst_data.cli_index rebuild-index --mode dry-run [--db PATH]
  ↳ MUST be strictly read-only: PRAGMA query_only=ON for the entire connection lifetime.
  ↳ classification/distribution display is moved to status output.
  ↳ Tier materialization (SET source_tier) is NEVER done here.
  ↳ Guard comparison is ARTICLE-SCOPED: article_l1_count == SELECT COUNT(*) FROM articles.
    Filing records are not counted — they have their own guard inside build_filing_records.

catalyst_data.cli_index materialize-tiers [--force] [--db PATH]
  ↳ NEW: Runs classify_articles (or full materialize with --force) and commits.
  ↳ --force: runs tier assignment over ALL rows, not just source_tier IS NULL.
  ↳ Prints: count updated, per-tier distribution, unknown publishers list.
  ↳ This is the ONLY command that writes source_tier.

catalyst_data.cli_index doctor [--json] [--db PATH]
  ↳ NEW: Runs P0 audit + embed-readiness + unknown-publisher check + dry-run zero-write assertion.
  ↳ --json: outputs JSON dictionary to stdout.
  ↳ Without --json: pretty-printed pass/fail summary.
  ↳ Exit 0 if all gates pass; exit non-zero (1) on any failure.
  ↳ Redacts all API keys/secrets to [REDACTED] in output.
  ↳ Opens DB with PRAGMA query_only=ON.
```

---

## Record Key Convention (Unified Accessor)

Article records have `article_id` and NO `corpus_item_id`. Filing records have `corpus_item_id` and `source_kind` and NO `article_id`. Any code iterating over mixed records must use a unified accessor:

```python
# Unified id for any record (article or filing):
record_id = r.get("corpus_item_id") or r["article_id"]
source_kind = r.get("source_kind", "article")
```

This pattern is used in `index_summary` (D1 fix) and wherever mixed records are processed.

---

## Canonicalization + Alias Table Design

### Problem
`tier_for_publisher("SeekingAlpha")` → 4 because `"SeekingAlpha"` != `"Seeking Alpha"`. Only `str.strip()` is applied.

### Design

**`_PUBLISHER_TIER` is case-insensitive** (N1): Keys are stored lowercase. Lookup lowercases the canonical name before exact-match. This means `_PUBLISHER_ALIASES` only needs spelling variants (whitespace/collapsed forms), not case mirrors.

```python
# _PUBLISHER_TIER keys become:
_PUBLISHER_TIER: dict[str, int] = {
    "marketwatch": 2,
    "globenewswire": 3,
    "globenewswire inc.": 3,
    "benzinga": 4,
    "investing.com": 4,
    "the motley fool": 5,
    "motley fool": 5,
    "zacks": 5,
    "zacks investment research": 5,
    "yahoo": 4,
    "yahoo finance": 4,
    "yahoo finance uk": 4,
    "yahoo finance video": 4,
    "seeking alpha": 5,
    "business wire": 3,
    "pr newswire": 3,
    "accesswire": 4,
    "tipranks": 5,
    "investor's business daily": 4,
    "the wall street journal": 2,
    "reuters": 2,
    "bloomberg": 2,
    "cnbc": 4,
    "fox business": 4,
    "barrons": 4,
    "morningstar": 4,
    "marketbeat": 4,
    "24/7 wall st.": 5,
}
```

`tier_for_publisher(name)`:
1. Call `canonicalize_publisher(name)` — returns None or canonical string
2. If None → return 4
3. `lookup = canonical.lower()`
4. If `lookup in _PUBLISHER_TIER` → return `_PUBLISHER_TIER[lookup]`
5. Else → return 4 (unknown)

**`_PUBLISHER_ALIASES`** — spelling variants only (whitespace, abbreviations, punctuation differences):

```python
_PUBLISHER_ALIASES: dict[str, str] = {
    "seekingalpha": "Seeking Alpha",
    "247 wall st.": "24/7 Wall St.",
    "247wallst": "24/7 Wall St.",
    "investors business daily": "Investor's Business Daily",
    "investorsbusinessdaily": "Investor's Business Daily",
    "wsj": "The Wall Street Journal",
}
```

A free function `canonicalize_publisher(name)` does:
1. If None/empty → return None
2. Strip leading/trailing whitespace
3. Collapse all internal whitespace runs to a single ASCII space
4. Unicode NFC normalization
5. Lookup lowercase result in `_PUBLISHER_ALIASES` (also lowercase keys)
6. If found → return alias value (canonical form with proper casing)
7. Else → return the NFC-normalized, whitespace-collapsed string

### Integration Points

**`tier_for_publisher(name) → int`** (unchanged return type):
- Calls `canonicalize_publisher(name)`
- Lowercases canonical name for `_PUBLISHER_TIER` lookup
- Returns tier int or 4

**`classify_articles(conn) → int`** (unchanged return type — per amendment):
- Updates only `WHERE source_tier IS NULL`
- Application-side loop: fetch NULL rows, compute canonical+tier per row, batch UPDATE per tier
- Returns count of rows updated (int)
- Does NOT collect unknown publishers — that's `unknown_publisher_audit`'s job

**`unknown_publisher_audit(conn) → dict`** (new function in source_tier.py):
- `SELECT DISTINCT publisher_name FROM articles`
- For each: canonicalize, check if canonical.lower() not in `_PUBLISHER_TIER`
- Returns `{"unknown_publishers": sorted(set), "count": int, "gate_passed": bool}`
- Reused by both `doctor` and `materialize-tiers` display

**`materialize_all_tiers(conn) → tuple[int, set[str]]`** (new):
- Runs tier assignment over ALL rows (no WHERE clause)
- Returns `(changed_count, unknown_publishers: set[str])`
- `changed_count` = rows where source_tier value actually changed

---

## Task 1: Fix D1 — `index_summary` KeyError (AMENDED)

**Files:**
- Modify: `packages/data-core/catalyst_data/index_builder.py` (index_summary function)
- Test: `packages/data-core/tests/test_index_builder.py` (add TestIndexSummaryPolymorphic class)

**What changes:**

`index_summary` currently:
```python
l2_eligible_ids = {r["article_id"] for r in l2}
total_article_ids = {r["article_id"] for r in l1}
```
Crashes on filing records (no `article_id` key). Also crashes on article records if changed to `corpus_item_id` (article records don't have that key).

**Fix:** Use unified accessor throughout.

New `index_summary` logic:
1. `l1 = [r for r in records if r["chunk_level"] == "l1"]`
2. `l2 = [r for r in records if r["chunk_level"] == "l2"]`
3. For each record in l1 and l2: `rid = r.get("corpus_item_id") or r["article_id"]` + `sk = r.get("source_kind", "article")`
4. `l2_eligible_ids`: set of `(rid, sk)` tuples for l2 records (avoids cross-kind collision)
5. `total_corpus_ids`: set of `(rid, sk)` for l1 records
6. Per-tier: nested dict `per_tier[source_kind][tier] = {"l1": N, "l2": N}`
7. Also emit flat `article_l1_count`, `article_l2_count`, `filing_l1_count`, `filing_l2_count`

Return dict shape:
```python
{
    "l1_count": int,           # total L1 across all source_kinds
    "l2_count": int,           # total L2
    "article_l1_count": int,   # article-only L1
    "article_l2_count": int,   # article-only L2
    "filing_l1_count": int,    # filing-only L1
    "filing_l2_count": int,    # filing-only L2
    "l2_eligible_count": int,  # unique (rid, sk) with L2 chunks
    "l2_eligible_pct": float,  # vs total unique (rid, sk)
    "would_embed_count": int,  # l1_count + l2_count
    "per_tier": dict,          # {source_kind: {tier: {"l1": N, "l2": N}}}
}
```

**Test list (TDD order):**

| # | Test | What It Catches |
|---|------|----------------|
| 1.1 | `test_summary_with_filing_records_does_not_crash` | KeyError on mixed records — passes filing records through index_summary |
| 1.2 | `test_summary_article_records_have_no_corpus_item_id` | Article records don't have corpus_item_id key → unified accessor works |
| 1.3 | `test_summary_mixed_article_and_filing` | DB with both article and filing records → correct per-kind counts |
| 1.4 | `test_summary_per_tier_split_by_source_kind` | per_tier has "article" and "filing" sub-keys |
| 1.5 | `test_summary_l2_eligible_filings` | l2_eligible_count includes filing items with long body |
| 1.6 | `test_summary_article_l1_count_matches_article_count` | article_l1_count == COUNT(*) FROM articles (for rebuild-index guard) |

---

## Task 2: Fix D2 — Dry-Run Writes

**Files:**
- Modify: `packages/data-core/catalyst_data/cli_index.py` (cmd_rebuild_index, new cmd_materialize_tiers, main parser)
- Modify: `packages/data-core/catalyst_data/source_tier.py` (add materialize_all_tiers, unknown_publisher_audit)
- Test: `packages/data-core/tests/test_cli_index.py` (TestCLIRebuildIndex, TestCLIMaterializeTiers)
- Test: `packages/data-core/tests/test_source_tier.py` (TestMaterializeAllTiers, TestUnknownPublisherAudit)

**What changes in `cmd_rebuild_index`:**

1. Open connection, then immediately: `conn.execute("PRAGMA query_only = ON")`
2. Remove `classify_articles(conn)` call entirely.
3. If any article has `source_tier IS NULL`, print WARNING (use `tier_distribution` to detect nulls), but continue.
4. `build_index_records(conn)` + `index_summary(records)` as before.
5. Guard comparison: use `summary["article_l1_count"]` (article-scoped) not the filing-inclusive `summary["l1_count"]`. Filing records have their own guard inside `build_filing_records`.

**State flow for `cmd_rebuild_index --mode dry-run`:**
```
_open_db(db_path)
  → conn.execute("PRAGMA query_only = ON")
  → dist = tier_distribution(conn)              -- read-only
  → if NULL key in dist: WARNING                -- no write
  → records = build_index_records(conn)          -- already read-only
  → summary = index_summary(records)             -- read-only
  → article_count = SELECT COUNT(*) FROM articles
  → at_count = SELECT COUNT(*) FROM article_tickers
  → ticker_ok = (ticker_refs == at_count)
  → dedup_ok = (summary["article_l1_count"] == article_count)
  → print summary with PASS/FAIL labels
  → if not (ticker_ok and dedup_ok): raise SystemExit(1)
  → conn.close()
```

**cmd_materialize_tiers (new in cli_index.py):**
```
cmd_materialize_tiers(db_path, force=False)
  → _open_db(db_path)  -- read-write allowed (no PRAGMA query_only)
  → if force:
      → changed, unknowns = materialize_all_tiers(conn)
      → print f"Materialized: {changed} rows updated (--force)"
  → else:
      → count = classify_articles(conn)  -- returns int
      → print f"Classified: {count} articles"
  → dist = tier_distribution(conn)
  → print per-tier counts
  → audit = unknown_publisher_audit(conn)
  → if audit["count"] > 0:
      → print "Unknown publishers (assigned T4):"
      → for each: print "  - {name}"
  → conn.close()
```

**materialize_all_tiers(conn) → tuple[int, set[str]] (new in source_tier.py):**
```
  → Fetch ALL articles: SELECT article_id, publisher_name, source_tier FROM articles
  → For each row:
      → canonical = canonicalize_publisher(publisher_name)
      → tier = _PUBLISHER_TIER.get(canonical.lower(), 4) if canonical else 4
      → if canonical and canonical.lower() not in _PUBLISHER_TIER:
          → unknown_set.add(canonical)
      → if tier != existing_source_tier:
          → changed_count += 1
      → UPDATE articles SET source_tier = ? WHERE article_id = ?
  → Commit once
  → Log unknown publishers via logger.warning
  → Return (changed_count, unknown_set)
```

**Test list (TDD order):**

| # | Test | What It Catches |
|---|------|----------------|
| 2.1 | `test_dry_run_does_not_write_source_tier` | classify_articles side-effect writes source_tier columns |
| 2.2 | `test_dry_run_pragma_query_only` | Connection allows writes despite dry-run name — any UPDATE raises sqlite3.OperationalError |
| 2.3 | `test_dry_run_warns_on_null_tiers` | NULL source_tier articles produce WARNING in output |
| 2.4 | `test_dry_run_db_sha_unchanged` | SHA256 of DB file before == after (catches ANY write, including VACUUM or WAL checkpoint) |
| 2.5 | `test_materialize_tiers_only_nulls` | Without --force: only NULL rows updated, count matches |
| 2.6 | `test_materialize_tiers_force_all` | With --force: every row re-evaluated; row with "SeekingAlpha" publisher → T5 |
| 2.7 | `test_materialize_tiers_idempotent` | Second --force run reports 0 changed |
| 2.8 | `test_classify_articles_returns_int` | classify_articles(conn) returns int (backward compat — live path in update_pipeline.py:780) |
| 2.9 | `test_materialize_all_tiers_returns_tuple` | materialize_all_tiers(conn) → (int, set) |
| 2.10 | `test_unknown_publisher_audit_detects_unknown` | unknown_publisher_audit returns non-empty list for DB with unknown publisher |
| 2.11 | `test_unknown_publisher_audit_empty_when_all_known` | All publishers in _PUBLISHER_TIER → empty list |

---

## Task 3: Fix D3 — Publisher Canonicalization (AMENDED)

**Files:**
- Modify: `packages/data-core/catalyst_data/source_tier.py` (add _PUBLISHER_ALIASES, canonicalize_publisher; revise _PUBLISHER_TIER keys to lowercase; revise tier_for_publisher; add unknown_publisher_audit, materialize_all_tiers)
- Test: `packages/data-core/tests/test_source_tier.py` (TestCanonicalizePublisher, TestMaterializeAllTiers, TestUnknownPublisherAudit)

**What changes:**

1. **Lowercase `_PUBLISHER_TIER` keys** (N1): All keys become lowercase. This eliminates the need for case-mirror aliases.

2. **Add `_PUBLISHER_ALIASES`** (spelling variants only — no case mirrors needed since tier lookup is case-insensitive). See table above.

3. **Add `canonicalize_publisher(name) → str | None`** as described above.

4. **Revise `tier_for_publisher(name) → int`** (return type unchanged):
   - Call `canonicalize_publisher(name)`
   - If None → return 4
   - `lookup = canonical.lower()`
   - Return `_PUBLISHER_TIER.get(lookup, 4)`

5. **Revise `classify_articles(conn) → int`** (return type unchanged — per amendment):
   - Switch from single-SQL CASE to application-side loop:
   - Fetch `SELECT article_id, publisher_name FROM articles WHERE source_tier IS NULL`
   - For each: canonicalize → compute tier → `UPDATE articles SET source_tier = ? WHERE article_id = ?`
   - Commit once at end
   - Return count of rows updated (int)
   - Does NOT log unknown publishers (that's `unknown_publisher_audit`'s job)

6. **Add `unknown_publisher_audit(conn) → dict`**:
   ```
     → SELECT DISTINCT publisher_name FROM articles WHERE publisher_name IS NOT NULL
     → For each: canonical = canonicalize_publisher(name)
     → if canonical and canonical.lower() not in _PUBLISHER_TIER:
         → add canonical to unknown_set
     → Return {"unknown_publishers": sorted(unknown_set),
               "count": len(unknown_set),
               "gate_passed": len(unknown_set) == 0}
   ```

7. **Add `materialize_all_tiers(conn) → tuple[int, set[str]]`**:
   - As described in Task 2 above.

**Test list (TDD order):**

| # | Test | What It Catches |
|---|------|----------------|
| 3.1 | `test_canonicalize_whitespace_collapse` | "Seeking  Alpha" → "Seeking Alpha" |
| 3.2 | `test_canonicalize_alias_resolution` | "seekingalpha" → "Seeking Alpha" (via alias) |
| 3.3 | `test_canonicalize_nfc_normalization` | Unicode composed/decomposed variants → same output |
| 3.4 | `test_canonicalize_none_empty` | None → None; "" → None |
| 3.5 | `test_tier_for_publisher_case_insensitive` | "SEEKING ALPHA" → 5, "seeking alpha" → 5 (N1: case-insensitive tier lookup) |
| 3.6 | `test_tier_for_publisher_uses_canonical` | tier_for_publisher("SeekingAlpha") == 5 |
| 3.7 | `test_tier_for_publisher_unknown_still_t4` | "TotallyRandomBlog!!!" → 4 |
| 3.8 | `test_classify_articles_returns_int` | classify_articles(conn) returns int (backward compat) |
| 3.9 | `test_classify_articles_canonicalizes` | "SeekingAlpha" publisher → T5 after classify |
| 3.10 | `test_materialize_all_tiers_changes_existing` | T4 record for "SeekingAlpha" → T5 after --force |
| 3.11 | `test_materialize_all_tiers_tracks_delta` | Returns correct changed_count |
| 3.12 | `test_materialize_all_tiers_returns_unknown_publishers` | Returns set of unknown canonical names |
| 3.13 | `test_unknown_publisher_audit_detects_unknown` | Non-empty list for unknown |
| 3.14 | `test_unknown_publisher_audit_empty_when_all_known` | Empty when all publishers recognized |

---

## Task 4: Fix D4 — Hard-Coded PASS Guards (AMENDED)

**Files:**
- Modify: `packages/data-core/catalyst_data/cli_index.py` (cmd_rebuild_index)
- Test: `packages/data-core/tests/test_cli_index.py` (TestCLIRebuildIndex)

**What changes:**

Replace:
```python
print(f"  Ticker-lossless guard:   PASS ({ticker_refs} == {at_count})")
print(f"  Dedup guard:             PASS (L1={summary['l1_count']} == articles={article_count})")
```

With computed checks that are **article-scoped** (per amendment):

```python
article_l1 = summary["article_l1_count"]   # from polymorphic summary
article_count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
at_count = conn.execute("SELECT COUNT(*) FROM article_tickers").fetchone()[0]
ticker_refs = sum(len(r["tickers"]) for r in l1_records if r.get("source_kind", "article") == "article")

ticker_ok = ticker_refs == at_count
dedup_ok = article_l1 == article_count
ticker_label = "PASS" if ticker_ok else "FAIL"
dedup_label = "PASS" if dedup_ok else "FAIL"

print(f"  Article ticker-lossless guard: {ticker_label} (records={ticker_refs} == DB={at_count})")
print(f"  Article dedup guard:           {dedup_label} (L1={article_l1} == articles={article_count})")

if not ticker_ok or not dedup_ok:
    raise SystemExit(
        f"Guard failure: ticker_lossless={ticker_ok}, dedup={dedup_ok}"
    )
```

Key change: `article_l1 == article_count` — not filing-inclusive `l1_count`. On a DB with filings, total `l1_count` would exceed `article_count` (filings add extra L1 records), causing a false FAIL. Article-scoped comparison prevents this.

**Test list (TDD order):**

| # | Test | What It Catches |
|---|------|----------------|
| 4.1 | `test_guard_failure_exits_nonzero` | Inject orphan article_ticker → rebuild-index exits 1, output shows FAIL |
| 4.2 | `test_guard_success_exits_zero` | Clean DB → exits 0, output shows PASS |
| 4.3 | `test_rebuild_index_exits_zero_with_filings` | DB with both articles AND filings → exits 0 (article-scoped guard, not filing-inclusive) |
| 4.4 | `test_guard_output_shows_fail_not_pass` | Output contains "FAIL" not "PASS" on violation |

---

## Task 5: New `doctor` Command

**Files:**
- Create: `packages/data-core/catalyst_data/doctor.py` (new module)
- Modify: `packages/data-core/catalyst_data/cli_index.py` (add cmd_doctor, parser subcommand)
- Test: `packages/data-core/tests/test_doctor.py` (new test file)

**What the doctor runs:**

```
doctor(db_path, json_output=False)
  → Compute DB SHA256 before any operations
  → Open conn with PRAGMA query_only = ON
  → Run audit_gate_p0(conn)              [from coverage_audit]
  → Run audit_embed_readiness(conn)      [from coverage_audit]
  → Run unknown_publisher_audit(conn)    [from source_tier]
  → Run dry_run_zero_write_assertion(db_path)  [new, in doctor.py]
  → Close conn
  → Compute DB SHA256 after (for dry-run assertion)
  → Assemble result dict
  → Redact secrets recursively
  → If --json: json.dumps to stdout
  → If not --json: pretty-print
  → sys.exit(0 if all_passed else 1)
```

**`dry_run_zero_write_assertion(db_path)` (in doctor.py):**
```
  → sha_before = sha256_file(db_path)
  → Open separate conn with PRAGMA query_only = ON
  → Run build_index_records(conn)  -- exercise the full dry-run code path
  → Run index_summary(records)
  → Close conn
  → sha_after = sha256_file(db_path)
  → Return {"db_sha_unchanged": sha_before == sha_after,
            "sha_before": sha_before,
            "sha_after": sha_after,
            "gate_passed": sha_before == sha_after}
```

**Secret redaction (`_redact_secrets(obj)`):**
- Walk the result dict recursively
- Any key containing "key", "secret", "token", "password", "api" (case-insensitive) → replace value with `"[REDACTED]"`
- Also scan string values for patterns like `sk-...`, `Bearer ...`, `api_key=...`
- Redact these in connection strings and env-var-like values
- Never log raw API keys

**Doctor output schema (--json):**
```json
{
  "doctor_version": "1.0.0",
  "generated_at": "2026-07-07T...",
  "db_path": "/path/to/db",
  "db_sha256": "abc123...",
  "all_gates_passed": false,
  "gates": {
    "gate_p0": { "gate_passed": true, "checks": [...], "failing": [] },
    "embed_readiness": { "gate_passed": true, "checks": [...], "failing": [] },
    "unknown_publishers": { "gate_passed": false, "count": 3, "unknown_publishers": [...] },
    "dry_run_zero_write": { "gate_passed": true, "db_sha_unchanged": true }
  },
  "exit_code": 1
}
```

**Pretty-print output (no --json):**
```
=== Doctor Report ===
  DB: /path/to/db
  SHA256: abc123...
  Generated: 2026-07-07T...

[PASS] Gate P0 (10/10 checks passed)
[PASS] Embed Readiness (6/6 checks passed)
[FAIL] Unknown Publishers (3 unknown)
  - UnknownPub LLC
  - RandomBlog
  - TestSource
[PASS] Dry-Run Zero-Write (DB SHA unchanged)

Result: 3/4 gates passed — EXIT 1
```

**Test list (TDD order):**

| # | Test | What It Catches |
|---|------|----------------|
| 5.1 | `test_doctor_all_gates_pass_on_clean_db` | Seeded clean DB with all known publishers → exit 0, all gates passed |
| 5.2 | `test_doctor_exits_nonzero_on_seeded_violation` | Inject an unknown publisher row → exit 1, unknown_publishers gate fails |
| 5.3 | `test_doctor_json_output_is_valid` | --json flag → stdout is parseable JSON with expected top-level keys |
| 5.4 | `test_doctor_json_redacts_secrets` | JSON output has no raw API key patterns; connection string values redacted |
| 5.5 | `test_doctor_dry_run_sha_unchanged` | DB SHA256 before and after are identical |
| 5.6 | `test_doctor_dry_run_sha_changed_detected` | If write occurs (simulated via separate write-conn pitfall) → sha_unchanged: false |
| 5.7 | `test_doctor_handles_missing_tables` | DB missing index_state table → graceful "TABLE_MISSING" in output, not crash |
| 5.8 | `test_doctor_output_contains_all_four_gates` | All four gate sections present in both JSON and pretty modes |
| 5.9 | `test_doctor_p0_failure_exits_nonzero` | Seeded P0 violation (e.g., blank reference_date) → exit 1 |
| 5.10 | `test_doctor_embed_readiness_failure_exits_nonzero` | Seeded embed readiness violation (e.g., dup L1 pending) → exit 1 |

---

## Execution Order & Dependency Graph

```
Task 3 (canonicalize + unknown_publisher_audit)
  ├──> Task 1 (index_summary polymorphic fix)
  │      └──> Task 4 (PASS guards, article-scoped)
  │
  ├──> Task 2a (extract tier from rebuild-index, add PRAGMA query_only)
  │      └──> Task 2b (materialize-tiers CLI command)
  │
  └──> Task 5 (doctor command — consumes all fixes)
         (depends on T3 for unknown_publisher_audit,
          T1 for summary invariants,
          T2a for dry-run contract)
```

**Recommended order: T3 → T1 → T2a → T2b → T4 → T5**

---

## LangSmith / Observability Notes

| Failure Mode | doctor Gate | doctor Output Signal | LangSmith Observable |
|---|---|---|---|
| publisher misclassified (SeekingAlpha→T4) | unknown_publishers | `"SeekingAlpha"` in unknown list | Per-tier distribution shows T4 inflation |
| dry-run writes to DB | dry_run_zero_write | `sha_unchanged: false` | DB row count change after CLI run |
| index_summary crash on filings | N/A (crash before doctor) | Traceback in stderr | Blocks doctor from running |
| source_tier NULL after materialize | Embed readiness E2 | `l1_equals_eligible: false` | eligible_article_count mismatch |
| orphan article_tickers | Gate P0 G6/G7 | canonical count violations | zero_canonical_dup_group > 0 |
| stale index_state rows | Embed readiness E3 | `one_l1_per_article: false` | dup_l1 > 0 |
| rebuild-index false FAIL on DB with filings | D4 guard article-scoped | EXIT 0 instead of EXIT 1 | Would cause false alarm pre-fix |

---

## Stage-Only Policy

All changes are staged (`git add`) but NOT committed. After each task:
```bash
git add <changed files>
git diff --cached --stat  # verify scope
```

No `git commit` until plan review is complete and execution is approved.

---

## Rollback & Safety

- Frozen DB `data/catalyst_eval_frozen_v2.db` (SHA `0dfc81b1…ecdcdf`) must never be opened for write. All reads use `PRAGMA query_only=ON`.
- Dev DB `data/catalyst_dev_ws4b.db` — dry-run never writes; `materialize-tiers` is the only new write path.
- Revert: `git checkout -- <file>` per task if needed.
