# Data Contract (data-core baseline v1.1)

> The historical documents named below are no longer in the workspace. Their full text is in git history under tag `research-archive-20261106`.


| Item | Content |
|---|---|
| Date | 2026-10-24 |
| Status | **v1.1 (2026-10-31); the published cutoff is the only breaking change, see §10**; v1.0 was finalized on 2026-10-30. The content is the original v0.3 draft; subsequent changes are recorded in §10 as v1.1, v1.2, and so on. This status does not mean implementation, consumer integration, or production deployment is complete. Open factual questions and consumer integration items are listed in §9. |
| Scope | Defines what the data layer provides to attribution: the shape of an evidence item, how its time is determined, point-in-time reads, deduplication, and gap reporting. Attribution reads data only through this contract, not directly from individual stores. |
| Related | Inventory · unattended operation · data readiness · correction tables in `data/corrections/` |

## 1. An Evidence Item

| Field | Type | Description |
|---|---|---|
| `evidence_id` | Text | Body: `web:` + the first 32 characters of sha256(normalized URL) (existing rule, unchanged); headline: `hl:<source>:<source-local id>` |
| `kind` | Enum | `body` (full text) · `headline` (headline and summary only) · `filing` (SEC filing exhibit or body) · `fact` (a value calculated by code, such as market data or an earnings difference) |
| `tickers` | List | Stocks mentioned in the article (source annotations plus company-name matches from the symbol table); see §7 for multiple share classes |
| `title`, `text` | Text | `text` is present only when `kind=body/filing` |
| `url`, `canonical_url` | Text | See §4 for normalization rules |
| `publisher`, `publisher_family` | Text | Examples: `Benzinga`, `benzinga.com` |
| `provider` | Text | Data provider, for example `finnhub`, `polygon`, `alpaca`, `sec`, `rss:prnewswire`, `tavily` |
| `source_class` | Enum | `issuer_disclosure` · `wire` · `news` · `aggregator` · `market_data` |
| `evidence_role` | Enum | Each source association is explicitly registered as `evidence` or `lead`; sources registered as leads must not appear in the evidence list |
| Time fields | See §2 | |
| `origin` | Text or null | Stable identifier for the original publisher; use the confirmed press-release ID for the same release. Keep it separate from provider and acquisition channel. When unknown or conflicting, set `origin` to null and `origin_status` to `unresolved` or `conflict`; do not create separate origins from headlines. |
| `observed_at` | UTC time | Time this system first obtained each body version. Do not substitute the article's `first_seen_at` for a later version's `observed_at`. |
| `association_observed_at` | UTC time | Time this system first associated the version with this ticker. In `observed` mode, it must also be no later than the cutoff. |
| `provider_updated_at` | UTC time or empty | Provider-declared modification time for this version; if missing, do not assume it was never modified |
| `is_backfill` | Boolean | Whether this is historical backfill; backfill does not change the actual `first_seen_at` |
| `provenance` | Enum | `legacy` (only migration cohorts explicitly registered in §3.1) or `versioned` (new archives and subsequent collection, following the full version rules); do not infer this from the record date |
| `migration_cohort_id` | Text or empty | Required for `legacy`; identifies the immutable migration manifest, old `eligible_at` rule version, and data snapshot |
| `record_key` | Text | Stable text form of the composite evidence key `(evidence_id, provider, version)`. Raw records from different providers for the same URL each have their own `record_key`; they coexist and do not overwrite one another. `evidence_id` remains unchanged (v1.1). |
| `content_hash` | Text | SHA-256 of the version's **original bytes**. Calculate the hash from the original bytes obtained at ingestion; do not fabricate a hash for an old version by cleaning its body again. Leave this field empty for old versions whose original text is missing (v1.1). |
| `content_state` | Enum | `full_text` · `snippet` · `headline` · `missing`. Separate from `kind`: having full text does not automatically make an item eligible as evidence (v1.1). |
| `usage_ceiling`, `usage_reason` | Enum, reason code | Maximum use assigned by data: `evidence` or `lead`, plus a reason code. Code determines the ceiling from registered (`provider`, `publisher`, `content_state`) combinations, not from the provider name alone. For example, only the combination provider=alpaca, publisher=Benzinga, content_state=full_text (§6) can reach `evidence`, and it must still pass completeness, point-in-time, version, and origin checks. Agents may downgrade this status but must not upgrade data's `lead` to `evidence` (v1.1). |
| `version` | Integer | Increment when content for the same `evidence_id` changes (Alpaca version table or page-declared modification time); point-in-time reads return only the version available by that time |

## 2. Time Fields

| Field | Meaning |
|---|---|
| `published_at` | Publication time (UTC, with Z). Determine it for each source according to §2.1. |
| `published_at_source` | Where this time came from: `sec_index` (EDGAR index page), `api`, `page` (page-declared time), `rss`, `wire_match` (time for the same item on a wire service), `correction` (correction table), `url_path` (date in URL path), or `provider_date` (provider gives date only). Precision for `url_path` and `provider_date` is fixed at `day`; these rows **are leads only**, must not have `eligible_at`, and use reason code `date_only`. They may be upgraded only when the page or provider supplies a timestamp (v1.1). Consumers must treat unknown values as not usable as evidence. |
| `published_at_precision` | `second` · `minute` · `day` · `unknown`. Evidence with day-only precision cannot determine whether it was before, during, or after market hours. |
| `first_seen_at` | Time we first retrieved it (UTC). This is a hard boundary in `observed` mode; `published` mode must not treat local retrieval time as historical availability. |
| `eligible_at` | Time used to determine whether an item falls within a session window. For `versioned`, use `published_at` and this contract's time rules. For `legacy` reads in `published` mode, preserve the original `eligible_at` rule fixed by the explicit migration manifest (§3.1); do not recalculate historical eligibility. |
| `time_flags` | List: `agree`, `api_offset` (known whole-hour offset, corrected), `suspected_tz` (two times differ by exactly 4 or 5 hours; store the later time), `disagree`, `imprecise`, `corrected:<correction-table-version>` |
| `api_published`, `page_published` | Preserve both original timestamps for audit |
| `eligible_at_original`, `eligible_at_source` | Original `eligible_at` before correction and its source (v1.1). Keep the corrected value in `eligible_at` and record `corrected:<version>` in `time_flags`; retain both values. A correction is a read view and does not rewrite frozen inputs. Adding correction-table entries, expanding deduplication relationships, or updating mappings does not change the interpretation of existing runs. Old runs, replays, and checkpoint restores read the original fixed versions. |
| `time_confirmation` | Closed set: `verified` · `unverified` · `conflict`, with `time_confirmation_reason` and the content version it applies to (`record_key`) (v1.1). Keep precision (`published_at_precision`) separate from confirmation status. Reason codes must at least distinguish `version_observation_missing` (version observation time missing) · `after_cutoff` (clearly after cutoff) · `modification_unconfirmed` (modification cannot be confirmed) · `date_only` (source provides date only) · `timezone_missing` (timezone missing) · `conflict_unresolved` (conflict unresolved). Do not collapse these into `EMPTY`. Retain existing `point_in_time_unverified` for `unverified`. |

### 2.1 Time Rules by Source

1. **SEC 8-K**: Use “Accepted” on the EDGAR filing index page (New York time) and convert it to UTC. Use `acceptanceDateTime` from the submissions interface only to screen candidates, because for about half of issuers the interface is 4 or 5 hours late (verified 2026-10-22: 119 of 226 were late; the rest matched). If the two differ by exactly 4 or 5 hours, record `api_offset`; record other mismatches as `disagree` and alert. Do not ingest records without a time on the index page; retry them later. Include daylight-saving test fixtures (v1.1): the repeated 01:00–02:00 hour on 2026-11-01 (US daylight-saving time ends), UTC conversions for the two adjacent days around 11-01, and 2027-03-14 (the reverse transition).
2. **Finnhub**: For CNBC and Benzinga, publication times are given by treating the US Eastern clock as UTC. If a page time exists, use the later of the API and page times. If no page time exists, mark `imprecise` and do not use as evidence (`times.SKEWED`).
3. **Alpaca (Benzinga wire)**: Use `created_at` to second precision; when content is updated, use `updated_at` and increment the version.
4. **RSS**: `pubDate` must include a timezone. If it does not, leave it empty; do not supply a default timezone.
5. **Page-declared times**: Accept only values with a timezone.
6. **Corrections**: Do not rewrite old ingested data. Apply corrected times in the read layer using the correction table, and record `corrected:<version>` in `time_flags`. The tables include `time_corrections_v1.csv` (Finnhub timezone) and `sec_acceptance_v1.csv` (SEC acceptance time).
7. **New sources**: Each newly integrated source must list an independent comparison source (`time_reference`) in the registry; a test checks this.

### SEC 8-K Acceptance-Time Correction Reads

`catalyst_data.read_api.sec_8k_acceptance_table(db_path, *, tickers=None, start=None, end=None, accessions=None, store_id="server:news_bodies_v2.sqlite3", correction_table_path=None, immutable=False)` queries `daily:sec` records in the body store using SQLite `mode=ro`. Set `immutable=True` for a static read-only copy; retain the default for a live database so SQLite can read the WAL. The caller supplies an accession list or one or more tickers and a UTC date range with inclusive start and end dates. Date queries first fetch candidates from SQL for one day before through one day after the requested range, then apply corrections and filter precisely by the UTC date of `corrected_at`. A candidate with no acceptance time raises `SecFilingDataError`; another record must not hide it. Apply corrections only to the exact `in_stores` scope in `sec_acceptance_v1.csv`.

The result includes accession, CIK, ticker, `form`, `filed_date`, `acceptance_at`, `corrected_at`, `correction_status`, `correction_ref`, the document URL list `exhibits`, and `missing_fields`. `correction_status` is one of `applied`, `none`, or `not_applicable_store`: when no correction applies, `corrected_at` equals `acceptance_at` and `correction_ref` is `none`; when the correction table has that accession but does not apply to the current store, `corrected_at` is `null`, `correction_ref` still points to the table row, and `corrected_at` is listed in `missing_fields`. Date queries retain such records when they fall within the expanded candidate range because their corrected date is unknown. No matching local record or a missing acceptance time raises `SecFilingDataError`; this means local data is insufficient, not that SEC has no filing.

The current `daily:sec` projection stores only accession, ticker, document URL, and acceptance time. It does not store filing date or 8-K/8-K/A form value separately. Therefore, `filed_date` and `form` are returned as `null` and included in `missing_fields`; do not infer these fields from the acceptance date or display title. This interface enumerates stored SEC body records and does not guarantee complete coverage of the filing index for the date range.

## 3. Point-in-Time Reads

```
as_of(ticker, window_start, window_end, at, *, mode, kinds=None) -> ReadResult   # at may be omitted in published mode
```

- `mode` is required and must be either `observed` or `published`; record it in every read result and run. Always label historical results as “historical query.”
- **`observed` (strict mode; for live production queries and prospective evaluation, caller must specify explicitly)**: Return only versions and ticker associations actually known at time `at`. Conditions: `version.observed_at <= at`, `first_seen_at <= at`, and `association_observed_at <= at`. No relaxed fallback is allowed: if any of these times is later than `at`, or association time is unknown, the item must not be returned as evidence **or included in the leads list**; record only a reason code in the audit (v1.1 clarification; existing implementation already behaves this way). Only items that were observed but have insufficient publication precision or confirmation are downgraded to leads. Historical versions missing version-level observation time are excluded from both evidence and leads; do not fabricate observation times. Boundary fixtures: open `window_start`, closed `window_end`, and equality when `first_seen_at = observed_as_of`. Records whose source role is `lead` may appear only in the separate leads list. Determine all other source eligibility according to the explicit labels in §6, not from the provider field.
- **`published` (historical query mode), branched by provenance**:
  - `legacy`: Preserve the original `eligible_at`, session window, source role, and existing exclusion rules only for old cohorts explicitly registered in §3.1. Do not downgrade an item to a lead solely because `provider_updated_at` is missing, and do not retroactively apply the new version-storage rules. Keep the `legacy` label explicit; do not claim that historical body versions have been proven unchanged. Continue to apply modification-time checks from the original rules.
  - `versioned`: New archives and subsequent collection require `published_at <= at`; using a body or filing as evidence also requires known `provider_updated_at <= at` and `evidence_role=evidence`. Versions modified after the cutoff are leads only. Backfilled content with unknown modification time is marked `point_in_time_unverified` and cannot be evidence verified for that point in time. Records with `evidence_role=lead` may appear only in the leads list.
- **The `published` cutoff is the session's `window_end` (v1.1)**: If `at` is omitted, use `window_end`; if `at` is later than `window_end`, raise an error. Do not relax the rule or silently substitute `window_end`. Therefore, a body modified after `window_end` is not returned as evidence (downgrade it to a lead with reason `modified_after_cutoff`). In `observed` mode, `observed_as_of` (that is, `at`) and `window_end` are separate values; provide both. Do not replace the `eligible_at` window condition with `first_seen_at`.
- Conditions shared by both modes: `tickers` contains the ticker; `window_start < eligible_at <= window_end`; use the last version allowed by the mode. In the `published` legacy branch, calculate eligible_at using the old rule fixed by the migration manifest; otherwise use this contract. The `legacy` label does not exempt a record from any `observed` time condition.
- Do not return evidence with empty `eligible_at`. Return leads through the separate `leads()` function and label them clearly as leads only.
- Read across all stores: daily stores, archives, and the evidence store. Hide store-specific differences from callers.
- Reproducibility requires pinning the data snapshot/version, correction-table version, contract version, and deduplication-rule version. In particular, historical backfill added later in `published` mode can change the visible set, so identical time parameters alone do not guarantee identical results forever. The read envelope and audit record include mode, cutoff, actual data identity, correction-table identity, provenance, and `migration_cohort_id`. Mark each item in mixed results and report counts by provenance; do not include `legacy` in the “point-in-time verified” set. Keep body and leads lists separate.
- **In `published` mode, if the current body was modified and the earlier pre-cutoff version is missing**, do not substitute the current body: return a lead or unknown with a reason code. If replay lacks the older body, raise `VERSION_BODY_UNAVAILABLE`; do not silently downgrade (v1.1).
- **Boundary**: This contract defines metadata for the data layer only. How app / agents record and use `mode` is outside this contract.
- **Version-row storage**: Each version row must retain the full body, content hash, `observed_at`, and `provider_updated_at`. The existing old `news_versions` table stores only timestamps and lengths, so the current snapshot alone cannot reconstruct overwritten old bodies. Backup recovery has not been verified, so do not claim that all copies are unrecoverable. Populate the affected count and the number recoverable from backups only after an authorized read-only snapshot review; do not invent values.

### 3.0 Read Envelope and Reproducible Identity (v1.1)

Each read returns a serializable identity that the caller stores unchanged in the run manifest:

- Contract version, `mode`, `observed_as_of`, window, snapshot creation time (wall clock, excluded from evidence hash), and optional `snapshot_id`;
- Correction table: version + full SHA-256 + applicable rule version. If disabled, explicitly set `enabled=false`; do not use an empty string;
- Deduplication: rule version, the actual `same_article` / `same_release` relationship set used (immutable snapshot), and its hash;
- Issuer identity: ticker/CIK historical mapping version, hash, and applicable effective dates; source-role registry version and hash;
- `migration_manifest_sha256` (both arms of the third version use the same migration manifest; do not determine `legacy` eligibility from article date or results after the fact);
- Actual evidence: ordered `record_key`, `version`, `content_hash`, `first_seen_at`/`observed_at`, `eligible_at`/precision/`time_confirmation`, `origin`, `usage_ceiling`.

Keep the three hashes separate: `evidence_set_hash` (covers the ordered evidence **and leads**, keeping the two sets separate and sorting each in a fixed order without dropping lead members), `data_read_identity_hash` (covers all identities above), and `model_input_hash` (calculated by agents; outside data's responsibility). Both parties must use the same serialization rules: UTF-8, keys in lexicographic order, `null` written as `null`, UTC time as `YYYY-MM-DDTHH:MM:SSZ`, evidence sort key `(eligible_at, store_id, source_id, document_id, version_id)`, and reject NaN. A hash alone is insufficient; recovery and replay must be able to retrieve the corresponding immutable content.

### 3.1 Explicit Migration Boundary for `legacy`

- Allowed cohorts: existing development set, T6, new earnings-day samples, and explicitly inventoried existing daily body records. “New earnings-day samples” means the existing sample set, not data from every future earnings date.
- The migration manifest must fix each store/snapshot path and SHA-256, table and record identity (or reproducible identity set), original `eligible_at` rule version, actual presence of collection and modification time fields, and assign a `migration_cohort_id`. “Before migration,” “before a certain date,” or a directory name alone does not define a cohort.
- Do not automatically label all pre-migration new archives (such as Alpaca archives) as `legacy`, or put future backfills of old-dated articles into `legacy`. Any archive not explicitly listed in the manifest and all subsequent collection use the `versioned` rules.
- Do not fabricate `first_seen_at`, `observed_at`, or `provider_updated_at`. In `observed` mode, do not return old versions that cannot be shown to have been known before the cutoff. In `published` mode, retain the old eligibility rules for `legacy` and prominently label it “historical query · legacy: uses old data eligibility rules; historical version completeness is unverified.”
- Preserve the fact that old version bodies are missing from the current snapshot; backup recovery has not been verified. Migration does not reconstruct bodies from length or time metadata. The number of records in each cohort, records missing modification times, affected overwritten versions, and backup recovery status are all **pending inventory**; there are no complete measured results, so do not cite estimated numbers.
- This section describes compatibility design; it does not change frozen evaluation data or the current attribution path. How agent/app displays, records, and uses these fields is outside this contract.

## 4. Cross-Source Linking and Deduplication (revised 2026-10-23; replaces the former title-plus-10-minute-window rule)

- **Principle**: Keep all raw records; do not delete them. `same_article` / `same_release` links connect records for deduplicated counts only.
- **`same_article` (same article)**: `evidence_id` is scoped by provider. Link records based on:
  1. Matching normalized URL / article identity (normalization: lowercase hostname; remove tracking parameters and fragments; map AMP to the original page);
  2. Record ID within the same provider.
- **`same_release` (same press release/publication)**: Use a confirmed press-release ID or a confirmed matching full-body fingerprint (normalization rules and version must be fixed). An SEC accession identifies a filing and does not by itself prove that different exhibits are the same press release. Matching wire datelines or matching body openings are also insufficient.
- **Headline similarity produces candidate links only**: Confirm with a body fingerprint or source reference (wire ID, same release ID) before linking. If only headlines are similar and there is no body or source evidence, keep the records separate.
- **Do not merge independent reports**: Independently written reports from different sources about the same event are not the same item; the data layer does not merge them. Count “multiple independent sources” by `origin`.
- **`origin` field**: Identifies the original publisher or release ID. For example, if a Benzinga item arrives through Alpaca and Finnhub, both use Benzinga as the original publisher; do not create a new independent origin for each article. If a press release arrives through PR Newswire, IR, and SEC 8-K EX-99.1, all use that press release as origin. Agents count sources by `origin`.
- **Relationship records (v1.1)**: Each `same_article` / `same_release` relationship includes a rule version, matching basis, **time the relationship was discovered**, and member `record_key` values. In `observed` mode, filter relationships, issuer mappings, and source roles by `observed_as_of` (a relationship discovered later cannot apply earlier). In `published` mode, use the fixed snapshot and validate mapping effective dates against the sample trading date. Both modes fix the actual set used and its hash (§3.0).
- **Assigning `origin`**: The same press release received through IR, a wire service, or SEC 8-K EX-99.1 uses the same `origin`. When origin is unknown or conflicting, set `origin` to null and `origin_status` to `unresolved` or `conflict`; **an unknown origin does not increase the independent-source count** (the agents enforce this; two unresolved items do not make two sources). See the next item for assignment rules. Cross-media merging for the same event remains the agents' responsibility; data does not do it.
- **`origin` values and use (v1.2)**: `origin` has only three prefix types: `release:` (confirmed release ID `release:<namespace>:<ID>`, or the same item confirmed by a full-body fingerprint `release:fp:<fingerprint-version>:<fingerprint-prefix>`), `pub:<publisher-family>` (a (`provider`, `publisher`, `content_state`) combination found in the publisher registry), and `issuer:cik:<10-digit CIK>` (the SEC filing itself; accession is not origin). Each record also has `origin_status` (`resolved` / `unresolved` / `conflict`); when `unresolved` or `conflict`, `origin` is `null`. **Agents may use only `origin_status` and whether two `origin` values are equal; do not parse the prefix**. Treat unknown `origin_status` values as not usable as independent sources. Assign by priority (release ID → body fingerprint → publisher registry → issuer CIK), taking the first available value without mixing priorities. If two different values occur at the same priority, mark `conflict`. When merging components, append origin-equivalence records (merge only within the same priority, choose the lexicographically smaller canonical value, and include discovery time and relationship ID): in `observed`, apply only relationships that were visible and whose equivalence was discovered before the read time; in `published`, apply only relationships in the fixed snapshot, using the same relationship set as the snapshot.
- **Time**: Correct each source timestamp according to §2.1 before comparison (for example, Benzinga times from Finnhub are 4 hours early); do not hide timestamp errors by widening time windows.
- SEC 8-K filings and EX-99.1 press releases retain separate identities and times. Create `same_release` only for a repost or exhibit confirmed to be the same press release; do not automatically treat the entire filing as a press release. Filing time remains the EDGAR acceptance time.
- **Validation (not yet completed)**: Before integration with attribution, manually review at least 50 candidate/control pairs, including accepted links, rejected links, and similar headlines with different original text. Fix sampling and labeling rules in advance, and report TP/FP/FN, sample recall, and false merges for each rule. Reviewing linked pairs alone estimates precision only; it cannot stand in for overall recall. There are no labeled results yet; report the method and results separately and do not cite invented numbers.

## 5. Gap Information

**Version 1 serves only ready ranges (v1.1)**: Add `ready_range()`, which returns a list of (ticker, session_date) pairs with the contract version and completeness basis (whether all six required categories are `complete`). `pairs` contains only pairs where all required categories are `complete`; put the rest in `incomplete_pairs` with a status for each category. A category that is required but has no declared requirement is `NOT_DECLARED` and counts as incomplete. `snapshot_id` uses the `rr1:` prefix and is derived from the manifest/store SHA, daily task store_id, coverage requirements and plan hash, required categories, cutoff time (UTC, microseconds), and contract version; if supplied by the caller, it must match the derived value. Admit pairs based only on required categories: declared categories not in the required set are still evaluated but do not affect admission. The invite link date selector and options list use only `pairs`. Daily collection separately creates a lightweight `ready_range.json` snapshot: its `pairs` are daily-verified `(ticker, session_date)` values with movers and do not check all six categories. P-7's optional ticker and date lists are read from this snapshot. `ready_range()` performs the stricter six-category completeness check and is used when connected to data reads. The `progress`, `deferred`, `breaker`, and other backfill fields in the table below are not used by agents / app in the first version; retain them.

**SEC request failures must be visible (v1.1)**: `sec_filings` reads return `ERROR` with a reason code and `attempted_at` when an index request fails; never return an empty result as `EMPTY`. Mark the retrieval time when using a cache hit.

**The six public categories are fixed**; do not expose provider names as categories to the frontend:

| `category` | Scope |
|---|---|
| `price_bars` | Target, peers, sector ETFs, and broad-market prices; use scope to distinguish subjects |
| `sec_filings` | SEC indexes, filings, and exhibits |
| `target_news` | News about the current target, including Alpaca/Benzinga full text fetched for the target's task |
| `peer_news` | News about the peers specified for this run, including their Alpaca/Benzinga full text |
| `web_search` | Web searches required for the run and cached coverage |
| `supplementary_news` | Other supplemental news, such as Kitco and industry/commodity leads; replaces the old name benzinga_kitco and does not mean all Benzinga items are supplemental |

data still stores detailed records internally by source and session, then projects them to the categories above (data-only delivery; app/SSE is not implemented here):

| Field | Description |
|---|---|
| `session_date`, `ticker`, `source_id`, `category`, `scope` | `source_id` is the registry identity; scope fixes target/peer/reference and the corresponding subject. Do not infer target/peer from provider. |
| `status` | `OK` · `EMPTY` · `ERROR` · `NOT_FETCHED` (not retrieved); OK/EMPTY describe only the request result and do not prove complete window coverage |
| `progress` | Backfill queue/running/retry/completion status, logical task ID, pending and completed target counts, and last update time |
| `reason` | `ERROR` (error category only), `DISABLED` (missing key or registered as disabled), `deferred` (host temporarily unavailable), `breaker` (circuit breaker); explicitly record items not required by this run's policy and do not present them as EMPTY |
| `coverage_until` | How far the successfully completed query window/cursor extends; do not use the latest article time as the coverage watermark. Record it with the window start and pagination completion status. |
| `mapping_version` | Version of the internal source-to-public-category mapping and the identity of this run's target/peer context |

For a logical task, `target_count` is the total number of sessions in the task and `completed_count` is a snapshot of sessions completed so far. Write the same snapshot to each session gap row for that task; consumers must not add `completed_count` across rows. Failed sessions do not count as completed. Completion means the collection task ended; it does not mean `coverage_*` proves complete coverage.

**SEC reopening rule**: Whether an SEC session is complete is determined by the latest attempt for that SEC source, ordered by `attempted_at`, then `attempt_id`. If the latest status is `ERROR` / `DISABLED`, it is pending again. This rule applies only to daily SEC collection and does not change completion decisions for other sources. If a later query finds a filing whose index-page time places it in a previously completed session, retain the old attempt and append a new one. If an exhibit fails, mark both the official-time session and the unresolved JSON-candidate session `ERROR` / `retrying`; retry them again next time. Reopening uses a new logical task ID so a terminal `completed` state does not change back to `retrying`. Commit the body, exhibit structure, intake attempt, and gap rows in the same SQLite transaction; roll back all of them if the write fails.

**Internal mapping implementation**: The versioned provider/source_id → category mapping is in `data/manifests/source_categories_v1.json`; `m9/source_categories.py` validates it against the source registry. Collection facts are written to `source_coverage_attempts`, `source_coverage_facts`, and `source_coverage_targets`; they record only source, product, actual subject, query range, and attempt, not the requested target/peer role. `m9.coverage_readiness.evaluate_coverage()` calculates completeness from each request's explicit requirements. `m9.gap_readiness.evaluate_snapshots(requirements, plan, verified_stores)` validates cross-store inputs and projects them to public categories. `category_gaps` and `GapLedger` remain for compatibility with the old ledger and are not the completeness basis for the new interface. Alpaca news coverage writes are implemented locally; integration of other sources is still pending. Production deployment and formal consumer integration are not complete.

Category aggregation follows the tasks declared by the caller and complete coverage: if any required subtask is missing, pagination is incomplete, or required range is uncovered, an OK from another provider cannot hide the gap. An approved fallback clears a gap only when it actually covers the same subject/range completely. Preserve raw source accounting, roles, and evidence eligibility; category aggregation does not change them. Data calculates completeness according to the rules below. How app/agent generates and transmits each request's requirements is outside this document.

**Requirement input and completeness evaluation (data-only)**: The caller explicitly declares a unique `requirement_id`, category, `session_date`, case ticker, scope, `subject_kind`/`subject_id`, `product`, request window, semantic filters, and internal `source_ids`. The ticker/instrument subject must match the requirement's scope_id; the case ticker is not used to match collection facts. A feed/query matches only its explicit identity; do not infer it from article labels. `CoveragePlan` explicitly lists current collection partitions, source capabilities, and fixed feed bindings; requirements do not duplicate collection hashes or pagination execution parameters. Sources must be mapped to this category and scope through the registry.

`source_policy=all` requires every source to be complete. `any` is only for explicit alternatives: one complete non-empty source can satisfy it, but a complete `EMPTY` result requires every alternative source to be complete and empty. For each relevant current subpartition, use the latest attempt's latest status and require all to be complete; the union of actual windows must cover the requested `(start,end]`. An overlapping successful partition cannot hide an incomplete partition. Complete enumeration requires `enumeration_complete=true`; unknown pagination/truncation remains null. A single RSS snapshot does not prove historical completeness. Market data also requires the queried session set to cover the required sessions; body data requires each explicitly enumerated, deduplicated body target to be retrieved. Evaluate headline, body, market data, and search products separately; an old row without product cannot prove headline or body completeness.

Public `results` are grouped by `(session_date,ticker,category)` and retain separate `products`. Each product lists its declared requirement, scope, request window, four public statuses, and completeness flag. A category is `complete=true` only when all its products and scopes meet requirements; explicitly list missing requirement IDs. Project internal PENDING/DISABLED to NOT_FETCHED, preserving the reason for DISABLED; OK/EMPTY alone still do not mean complete. Progress counts apply only to the declared requirements in this request; do not sum across tasks or invent scheduler queue/running status from read results. Do not generate undeclared categories.

Cross-store inputs must be created by `storage.snapshots.load_verified_stores()` after checking the input manifest, paths, SHA, and environment. Verify them again before and after reads and use read-only transactions; use actual SHA values in audit `data_snapshot_ids`. The same SHA can correspond to different logical store_id values. Exact duplicate history can be deduplicated; conflicting history or duplicate store_id values raise an error. Do not invent global ordering from store-local sequence numbers.

Consumer integration for data readiness (RG-02) is handled by the agent/app session; how gaps affect answers remains governed by its existing policy. The data layer does not override the publishing path.

## 6. Source Roles in Attribution

**Evidence-source boundary**: Of newly integrated sources, only full-text Benzinga obtained through Alpaca may be used as evidence. Register roles by specific source and original origin. Record source reliability tier separately from evidence role. Content from other publishers returned by Alpaca does not automatically qualify as evidence just because the provider is Alpaca:
- Business Wire: the body page returns 403; do not bypass it. RSS metadata is a lead only. Content obtained through another approved channel is evaluated under that channel's rules; do not create a new Business Wire body-evidence path.
- PR Newswire: store metadata only (RSS); do not store body text.
- Seeking Alpha: determine usable sections together with source tiering (B7); until then, leads only.
- Marketaux, NewsAPI, GDELT: leads only.

All sources in the trial use one evidence store for common accounting, time (`timefields`), deduplication (§4), monitoring, and backups. **Reliability tier** (core / supplemental / lead only / retired) and **evidence role** (usable as evidence / lead only) are independent dimensions. Record and assess them separately; do not reject integration solely because volume is low.

## 7. Issuer Identity

- CIK is the issuer's primary key. Map tickers as of the relevant time; maintain historical mappings for renames and ticker changes (RQ-18).
- Multiple share classes (BRK-A/B, GOOGL/GOOG, FOX/FOXA): share news and filings by CIK; use each ticker for market data. Normalize both `.` and `-` spellings to `-` (RQ-17).

## 8. Acceptance

- Test every rule: time rules, point-in-time reads (including `first_seen_at` boundaries and versions), deduplication, the gap table, and corrections for known SEC and Finnhub errors.
- Replay one development-set session: compare the evidence set read under this contract with the existing read method and explain each difference.
- Include the contract version in each run manifest.

## 9. Explicit Open Items

The following facts, manual checks, or downstream interface details are still missing. They do not change implementation rules already defined in §1–8; do not fill gaps with guesses:

- **Actual `legacy` cohorts**: Inclusion rules are set. Do not label a cohort `legacy` until each store's path, snapshot SHA-256, record identity, old `eligible_at` rule, and backup recovery status have been confirmed. Keep the unknown number of lost bodies unknown.
- **Correction-table integration**: A pure decision module has been added that validates CSVs by SHA-256, queries exact store scope, and does not rewrite databases; it is not yet integrated into `as_of`. Until integration is complete, do not claim Finnhub/SEC correction rules work end to end, and do not compare raw timestamps to cutoffs before correction.
- **`versioned` records missing `provider_updated_at`**: They cannot be point-in-time verified evidence in `published` mode. Mark backfill as `point_in_time_unverified` and use it only as a lead. Non-backfill records also cannot be verified evidence; retain the “modification time unknown” reason. More detailed downstream display text is outside this document.
- **Source reliability tiers**: Set core/supplemental/lead-only/retired tiers using trial data, target 10-29 PT. Keep evidence eligibility separate from reliability; do not change evidence roles already established in §6.
- **Deduplication quality results**: Manual labeling of at least 50 pairs is not complete. Before labeling is complete, do not report recall, false-merge rate, or overall accuracy, and do not promote similar-headline candidates to confirmed links.
- **Stable `origin` assignment algorithm** (added in v1.1): Values and use are defined in §4 (v1.2); implementation is in `dedup/origin_assign.py`. Sample review is still required before agent integration because this changes what agents read.
- **Consumer transport DTO and production integration**: This contract defines data-only fields and read semantics, not the specific app/SSE transport shape. App/agent/eval integration and production cutover are outside this document.

## 10. Change Log

For each change, state what changed, whether it is compatible (whether old consumers remain correct without code changes), and who it affects. Breaking changes must be reviewed before integration.

| Version | Date | Change | Compatibility | Impact |
|---|---|---|---|---|
| v1.0 | 2026-10-30 | Finalized. Content = original v0.3 draft, with no wording changes | Not applicable | All consumers use this as the baseline |
| v1.1 | 2026-10-31 | Added the read envelope and three hashes (§3.0); closed set of point-in-time confirmation statuses and reason codes (§2); `record_key`, `content_hash`; `content_state`, `usage_ceiling`, `usage_reason`; `url_path`/`provider_date` are leads only; relationship records and unknown-origin handling; retain `eligible_at` before and after correction; daylight-saving fixtures; SEC request failures must return `ERROR`; replay fails when old body is missing; `ready_range()` and backfill-field labels; `migration_manifest_sha256`; in `observed`, versions unknown or later than cutoff are not returned even as leads (clarification; existing implementation already behaves this way) | Compatible: all are new fields or clarifications; unknown enum values are not usable as evidence | All consumers; the read interface needs additional fields |
| v1.1 | 2026-10-31 | **Changed the `published` cutoff to the session's `window_end`**: if `at` is omitted, use `window_end`; if `at` is later, raise an error | **Breaking** (the only v1.1 breaking change): previously, `published` used `at`; calls with `at` after `window_end` now error. Bodies modified after `window_end` are no longer evidence. There are currently no consumers. | `temporal_evidence.as_of`, `as_of_stores`, `read_api.as_of` |
| v1.2 | 2026-11-01 | Restrict `origin` prefixes to `release:`, `pub:`, and `issuer:cik:`; use `null` for `unresolved`/`conflict`; agents use only `origin_status` and origin equality and do not parse prefixes (§4) | Compatible: adds constraints on field values; old consumers do not read origin; there are currently no consumers reading origin | agents (independent-source count); `dedup/origin_assign.py` |
