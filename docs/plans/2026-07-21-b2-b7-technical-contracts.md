# Catalyst B2–B7 Technical Contracts

- Status: binding implementation contract
- Scope: Backend + API packages B2–B7
- Audience: implementation-plan authors, executors, and reviewers
- Supersedes: conflicting counts, migration numbers, formulas, or sequencing in older plans and research notes
- Does not supersede: product positioning, package boundaries, provenance principles, or explicit non-goals in the canonical designs

## 1. Authority and execution order

The binding document order is:

1. this technical contract for formulas, state machines, identities, migration ownership, and B2–B7 sequencing;
2. `2026-07-19-catalyst-open-source-workbench-design.md` for product behavior and the Core Exit Gate;
3. `2026-07-19-catalyst-final-package-architecture.md` for package ownership and dependency direction;
4. `2026-07-19-catalyst-provider-provenance-and-chunking-design.md` for provider lineage and chunk profiles;
5. `2026-07-19-catalyst-evaluation-architecture-review.md` for evaluation surfaces and result-pack governance;
6. `2026-07-21-catalyst-roadmap.md` for package order and scope.

Execution order remains B2 → B3 → B4 → B5 → B6 → B7. The Eval Foundation scaffold is created during B4 before any retrieval pool is persisted. B6 produces all retrieval-arm outputs and the union judgment pool. B7 performs human grading, freezes labels, computes metrics, makes component keep/kill decisions, and releases the backend.

## 2. Cross-package invariants

1. `catalyst-agents` may depend on `catalyst-data`; it never imports or depends on `catalyst-eval`.
2. `catalyst-eval` consumes plain state, trace, assurance, and result-pack artifacts through its own adapters.
3. Metadata filtering, especially `available_at <= cutoff`, occurs before scoring on every retrieval path.
4. A raw provider response is not a searchable document. Normalization creates independent domain records before chunking.
5. No implementation may write to `data/catalyst_eval_frozen_v2.db`. The realpath guard executes before opening a writable connection or running any PRAGMA.
6. Provider credentials, signed URLs, authorization headers, cookies, and raw secret-bearing cursors never enter logs, traces, request ledgers, reports, or API responses.
7. Every hash described below uses UTF-8 canonical JSON: sorted object keys, compact separators, stable list ordering where the list is semantically unordered, UTC ISO-8601 timestamps, and no NaN/Infinity values.
8. Runtime timestamps and local paths do not enter deterministic identities unless explicitly listed.

## 3. Migration ownership

The data-core migration registry currently contains v1–v7. The current Dev DB may still report `PRAGMA user_version=6`; applying v7 before later migrations is normal and must be tested on a copy.

| Version | Owner | Schema responsibility |
|---|---|---|
| v7 | existing OHLCV execution code | `source_checkpoints.empty_reason` |
| v8 | B2 | request-attempt ledger, request-scoped raw-response fields, normalized provenance, durable update-run control |
| v9 | B3 | corpus documents/chunks, profile identity, corpus manifests, reconciliation and tombstones |
| v10 | B4 | FTS5 lexical index and lexical-index metadata if persistent schema is required |

B5 does not add financial attribution tables to the corpus DB for the Core milestone. Benchmark, sector ETF, peer, and relationship configuration are versioned manifests plus existing OHLCV/domain records. B5 may add an additive trace-schema version for assurance records, but the agents trace DB must first gain an explicit schema-version registry; it must not borrow data-core `user_version` numbers.

B6 index manifests live with the retrieval index and corpus manifest. B7 benchmark labels and ResultPacks are versioned files, not corpus DB migrations.

## 4. B2 — Data update and provenance

### 4.1 Plan identity

`UpdatePlan` contains the requested universe, date window, stages, provider profiles, source list, request budgets, fallback policy, and deterministic cells.

```text
plan_hash = SHA256(canonical_json(plan_without_runtime_fields))
```

Excluded fields: `created_at`, `run_id`, execution progress, report path, PID, heartbeat, lease owner, and measured latency. Included fields: ticker/series universe, source and endpoint, start/end, stage order, calendar version, provider profile version, request/page caps, fallback chain, peer tier, and configuration version.

```text
cell_id = SHA256(canonical_json({
  source_type, endpoint_name, ticker_or_series,
  window_start, window_end, stage, provider_profile_version
}))

logical_fetch_id = SHA256(run_id + ":" + cell_id)
```

Starting execution re-plans from current durable state. If the new hash differs from the confirmed `expected_plan_hash`, execution raises `PlanDriftError` before the first write or network request.

### 4.2 Request identity

Each actual HTTP attempt/page has a unique `request_id`. Retries and pages share `logical_fetch_id` but never overwrite one another.

```text
request_fingerprint = SHA256(canonical_json({
  method, normalized_host, normalized_path,
  sorted_redacted_params, request_body_sha256,
  provider_profile_version
}))
```

Secret fields are removed before this object can be logged or persisted. A pagination cursor is stored only as a fingerprint unless its provider contract proves it contains no credential or personal data.

### 4.3 State machines

```text
Update run:
PLANNED
  -> RUNNING_OHLCV
  -> RUNNING_EVIDENCE
  -> SUCCEEDED | PARTIAL | FAILED | CANCELLED

Logical fetch:
PLANNED
  -> RUNNING
  -> SUCCEEDED | SUCCESS_EMPTY | PARTIAL | FAILED | CANCELLED

Request attempt:
STARTED
  -> SUCCEEDED | HTTP_ERROR | TRANSPORT_ERROR | TIMEOUT |
     RATE_LIMITED | AUTH_ERROR | PARSE_ERROR | CANCELLED
```

`CANCEL_REQUESTED` is a persisted run flag, not a terminal result. Cooperative cancellation is checked before a cell, between pages, before fallback, and before commit. Successful pages followed by a failed/cancelled page produce `PARTIAL`, not `SUCCEEDED`. `SUCCESS_EMPTY` requires a valid provider response whose domain result is genuinely empty.

### 4.4 Two-stage execution

1. execute OHLCV cells;
2. commit successful OHLCV cells atomically;
3. re-plan evidence cells using the refreshed trading-session watermark;
4. compare the refreshed plan to the confirmed contract;
5. execute news, filing, macro, and fundamental cells;
6. persist a terminal run report.

An explicit `allow_stale_ohlcv` override is recorded in the plan and report. It is never inferred silently.

### 4.5 Raw response and normalized provenance

One HTTP response/page creates one append-only request-scoped raw row. The stored body is the HTTP-client-decoded byte body; the original `Content-Encoding` header is recorded separately. An existing request ID with the same response hash is an idempotent no-op; the same ID with different bytes is an integrity error.

One raw response containing N valid news items creates N canonical articles. `normalized_provenance` records every entity-version-to-raw relationship. A partial logical fetch may normalize successful pages, but all downstream context carries a coverage-degraded flag.

### 4.6 Migration v8 DDL contract

Migration v8 is additive for legacy data. New B2 rows use the constraints below; existing v1 `raw_assets`, legacy ingestion runs, and legacy checkpoint rows remain readable. The migration is executed only on temporary databases or an explicitly authorized disposable Dev DB copy during implementation.

#### `provider_request_attempts`

```sql
CREATE TABLE provider_request_attempts (
    request_id                  TEXT PRIMARY KEY,
    run_id                      TEXT NOT NULL,
    logical_fetch_id            TEXT NOT NULL,
    source_type                 TEXT NOT NULL,
    provider                    TEXT NOT NULL,
    endpoint_name               TEXT NOT NULL,
    ticker_or_series            TEXT NOT NULL,
    window_start                TEXT NOT NULL,
    window_end                  TEXT NOT NULL,
    attempt_no                  INTEGER NOT NULL CHECK (attempt_no >= 1),
    page_no                     INTEGER NOT NULL CHECK (page_no >= 1),
    parent_request_id           TEXT,
    request_fingerprint         TEXT NOT NULL
        CHECK (length(request_fingerprint) = 64
               AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
    request_params_redacted     TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(request_params_redacted)),
    cursor_fingerprint          TEXT
        CHECK (cursor_fingerprint IS NULL OR
               (length(cursor_fingerprint) = 64
                AND cursor_fingerprint NOT GLOB '*[^0-9a-f]*')),
    started_at                  TEXT NOT NULL,
    completed_at                TEXT,
    status                      TEXT NOT NULL CHECK (status IN (
        'STARTED', 'SUCCEEDED', 'HTTP_ERROR', 'TRANSPORT_ERROR',
        'TIMEOUT', 'RATE_LIMITED', 'AUTH_ERROR', 'PARSE_ERROR', 'CANCELLED'
    )),
    http_status                 INTEGER CHECK (
        http_status IS NULL OR http_status BETWEEN 100 AND 599
    ),
    latency_ms                  REAL CHECK (latency_ms IS NULL OR latency_ms >= 0),
    items_count                INTEGER CHECK (items_count IS NULL OR items_count >= 0),
    retry_after_seconds         REAL CHECK (
        retry_after_seconds IS NULL OR retry_after_seconds >= 0
    ),
    rate_limit_remaining        INTEGER CHECK (
        rate_limit_remaining IS NULL OR rate_limit_remaining >= 0
    ),
    provider_request_id         TEXT,
    error_class                 TEXT,
    error_message_redacted      TEXT,
    raw_asset_id                TEXT,
    response_sha256             TEXT CHECK (
        response_sha256 IS NULL OR
        (length(response_sha256) = 64
         AND response_sha256 NOT GLOB '*[^0-9a-f]*')
    ),
    response_bytes              INTEGER CHECK (
        response_bytes IS NULL OR response_bytes >= 0
    ),
    UNIQUE (logical_fetch_id, attempt_no, page_no),
    FOREIGN KEY (run_id) REFERENCES ingestion_runs(run_id) ON DELETE RESTRICT,
    FOREIGN KEY (parent_request_id) REFERENCES provider_request_attempts(request_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (raw_asset_id) REFERENCES raw_assets(asset_id) ON DELETE RESTRICT
);

CREATE INDEX idx_request_attempts_run
    ON provider_request_attempts(run_id, started_at, request_id);
CREATE INDEX idx_request_attempts_fetch
    ON provider_request_attempts(logical_fetch_id, page_no, attempt_no);
CREATE INDEX idx_request_attempts_status
    ON provider_request_attempts(status, provider, started_at);
```

Insertion requires `status='STARTED'`. Identity fields (`request_id`, `run_id`, `logical_fetch_id`, provider/endpoint/window fields, attempt/page numbers, parent ID, request fingerprint, and redacted params) are immutable. The only legal status update is `STARTED → one terminal request status`; a terminal row is immutable. The transition update sets `completed_at` and may set only response/error/latency/rate-limit fields. SQLite triggers enforce all three rules.

#### `normalized_provenance`

```sql
CREATE TABLE normalized_provenance (
    entity_type        TEXT NOT NULL CHECK (entity_type IN (
        'article', 'filing', 'macro_observation', 'ohlcv',
        'fundamental_snapshot'
    )),
    entity_id          TEXT NOT NULL,
    entity_version     TEXT NOT NULL
        CHECK (length(entity_version) = 64
               AND entity_version NOT GLOB '*[^0-9a-f]*'),
    raw_asset_id       TEXT NOT NULL,
    normalizer_version TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id, entity_version, raw_asset_id),
    FOREIGN KEY (raw_asset_id) REFERENCES raw_assets(asset_id) ON DELETE RESTRICT
);

CREATE INDEX idx_normalized_provenance_raw
    ON normalized_provenance(raw_asset_id, entity_type, entity_id);
CREATE INDEX idx_normalized_provenance_entity
    ON normalized_provenance(entity_type, entity_id, entity_version);
```

`entity_version` is the SHA-256 hex digest of canonical JSON containing exactly the normalized fields written for that entity version. It is not a timestamp or mutable row version. Re-recording the same edge is an idempotent no-op; a different raw row may legitimately point to the same entity version.

#### Additive columns and v2 raw-row rules

```sql
ALTER TABLE raw_assets ADD COLUMN response_sha256 TEXT;
ALTER TABLE raw_assets ADD COLUMN request_id TEXT;
ALTER TABLE raw_assets ADD COLUMN page_no INTEGER;
ALTER TABLE raw_assets ADD COLUMN content_encoding TEXT;
CREATE UNIQUE INDEX idx_raw_assets_request_id
    ON raw_assets(request_id) WHERE request_id IS NOT NULL;

ALTER TABLE source_checkpoints ADD COLUMN logical_fetch_id TEXT;
ALTER TABLE source_checkpoints ADD COLUMN request_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_checkpoints ADD COLUMN pages_received INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_checkpoints ADD COLUMN items_received INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_checkpoints ADD COLUMN is_complete INTEGER NOT NULL DEFAULT 0;

ALTER TABLE ingestion_runs ADD COLUMN plan_hash TEXT;
ALTER TABLE ingestion_runs ADD COLUMN expected_plan_hash TEXT;
ALTER TABLE ingestion_runs ADD COLUMN allow_stale_ohlcv INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ingestion_runs ADD COLUMN allow_stale_ohlcv_overridden INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ingestion_runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ingestion_runs ADD COLUMN lease_holder TEXT;
ALTER TABLE ingestion_runs ADD COLUMN lease_expires_at TEXT;
ALTER TABLE ingestion_runs ADD COLUMN parent_run_id TEXT;
```

Because SQLite cannot add every CHECK/FK constraint to an existing table with `ALTER COLUMN`, v8 adds triggers with these exact rules:

- a new request-scoped raw row has `data_version='v2'`, `asset_id='raw:' || request_id`, non-null `request_id`, `response_sha256`, `page_no >= 1`, and `content_encoding`; when the HTTP response has no content-encoding header, persist the literal value `identity`;
- `response_sha256` is lowercase 64-hex and equals the decoded `content_raw` bytes;
- a v2 raw row rejects UPDATE and DELETE; legacy rows with `request_id IS NULL` keep legacy compatibility behavior;
- checkpoint counters are non-negative, `is_complete` is 0/1, and a completed checkpoint has non-null `logical_fetch_id`;
- v2 ingestion flags are 0/1; `plan_hash` and `expected_plan_hash` are lowercase 64-hex when present;
- a v2 ingestion run is inserted as `PLANNED`; legal transitions are `PLANNED → RUNNING_OHLCV|RUNNING_EVIDENCE|CANCELLED`, `RUNNING_OHLCV → RUNNING_EVIDENCE|PARTIAL|FAILED|CANCELLED`, and `RUNNING_EVIDENCE → SUCCEEDED|PARTIAL|FAILED|CANCELLED`; terminal v2 runs cannot transition again.

Trigger names and failure codes are binding so migration drift is machine-checkable:

| Trigger | Timing/event | Reject condition | `RAISE(ABORT, ...)` code |
|---|---|---|---|
| `trg_raw_assets_v2_insert_guard` | BEFORE INSERT | any v2 raw-row rule above is false | `raw_asset_v2_contract` |
| `trg_raw_assets_v2_update_guard` | BEFORE UPDATE | `OLD.request_id IS NOT NULL` | `raw_asset_v2_immutable` |
| `trg_raw_assets_v2_delete_guard` | BEFORE DELETE | `OLD.request_id IS NOT NULL` | `raw_asset_v2_immutable` |
| `trg_request_attempt_insert_guard` | BEFORE INSERT | initial status is not `STARTED` | `request_attempt_initial_status` |
| `trg_request_attempt_identity_guard` | BEFORE UPDATE | any immutable identity field changes | `request_attempt_identity_immutable` |
| `trg_request_attempt_transition_guard` | BEFORE UPDATE | old status is terminal, new status is not terminal, or completion fields violate the transition contract | `request_attempt_illegal_transition` |
| `trg_request_attempt_delete_guard` | BEFORE DELETE | always | `request_attempt_append_only` |
| `trg_checkpoint_v2_insert_guard` | BEFORE INSERT | counters/boolean/completion rules above are false | `checkpoint_v2_contract` |
| `trg_checkpoint_v2_update_guard` | BEFORE UPDATE | counters/boolean/completion rules above are false | `checkpoint_v2_contract` |
| `trg_ingestion_run_v2_insert_guard` | BEFORE INSERT | a row with non-null `plan_hash` is not inserted as `PLANNED` or violates hash/boolean rules | `ingestion_run_v2_contract` |
| `trg_ingestion_run_v2_transition_guard` | BEFORE UPDATE | status edge is outside the declared graph, identity hashes change, or a terminal row changes | `ingestion_run_illegal_transition` |

For request attempts, terminal means every declared status except `STARTED`; a legal terminal update requires non-null `completed_at`, may set only the response/error/latency/rate-limit fields named in the table contract, and cannot later change any column. For v2 ingestion rows, `plan_hash`, `expected_plan_hash`, `run_id`, and `parent_run_id` are immutable after insert. `parent_run_id` is null for a fresh run and equals the existing parent run for resume; self-reference or a missing parent is rejected by `trg_ingestion_run_v2_insert_guard`. Tests assert both trigger names from `sqlite_master` and exact abort codes; implementations may factor SQL generation, but may not weaken predicates.

The existing storage layer persists checkpoint statuses in lowercase. The domain-to-storage mapping is binding:

```text
PLANNED       -> pending
RUNNING       -> running
SUCCEEDED     -> success
SUCCESS_EMPTY -> success_empty
PARTIAL       -> partial
FAILED        -> failed
CANCELLED     -> cancelled
SKIPPED       -> skipped
```

Migration v8 updates the checkpoint validator/rebuild path so these eight lowercase values are accepted. New code does not mix uppercase domain values and lowercase storage values in the same field.

## 5. B3 — Corpus and chunking

### 5.1 Searchable boundary

Only canonical eligible documents are chunked. Raw JSON, provider envelopes, URLs, image bytes, OHLCV arrays, FRED observation arrays, FMP statement payloads, and SEC submissions manifests emit zero searchable chunks.

### 5.2 `news_v2`

Input text is `title + "\n" + description` after canonical normalization.

- text ≤ 384 pinned-tokenizer tokens: exactly one searchable chunk;
- longer text: sentence/paragraph-aware target of 320 tokens with at most 48 tokens overlap;
- effective nominal stride is 272 tokens, but semantic boundary selection may shorten a chunk; it may never exceed 384 or overlap by more than 48;
- a repeated title prefix counts against the 384-token ceiling;
- no searchable whole-parent vector when searchable children exist;
- image URL, article URL, publisher, logo, and ticker JSON remain metadata.

#### Deterministic semantic-window algorithm

`news_v2` and `filing_v2` use this exact algorithm; implementations may not substitute another sentence splitter or boundary heuristic.

1. Normalize text with Unicode NFC, convert CRLF/CR to LF, strip trailing horizontal whitespace per line, collapse runs of spaces/tabs inside a line to one space, collapse three-or-more consecutive newlines to two, and strip the full result.
2. Tokenize with `TOKENIZER_MODEL_ID="BAAI/bge-m3"`, `TOKENIZER_REVISION="5617a9f61b028005a4858fdac845db406aefb181"`, `add_special_tokens=False`, and offset mappings enabled. Token counts and boundaries always use this pinned tokenizer.
3. Semantic boundary candidates are derived from normalized character offsets and converted to token-end offsets:
   - paragraph boundary: end of a non-empty paragraph immediately before `\n\n` or end of text;
   - sentence boundary: end of `[.!?。！？]`, followed by zero or more closing quotes/brackets and then whitespace or end of text.
4. For a short news document where `count_tokens(title + "\n" + description) <= 384`, emit that exact normalized text as one chunk with `section_key="body"`, ordinal `0001`, and no repeated prefix.
5. For a windowed document, the repeated prefix is the normalized title or section heading truncated at a tokenizer offset to at most 64 tokens, followed by one LF. The prefix is included in every chunk and counts toward both the 320 target and 384 maximum. `prefix_truncated=true` is metadata when truncation occurs.
6. Let `P` be prefix tokens, `body_target=max(1, 320-P)`, `body_max=max(1, 384-P)`, `s` the current body-token start, `target=min(N, s+body_target)`, `hard=min(N, s+body_max)`, and `lower=max(s+1, target-48)`.
7. Select the end `e` deterministically:
   - if `N <= hard`, choose `N`;
   - otherwise choose the paragraph boundary in `[lower, hard]` minimizing `abs(boundary-target)`;
   - if none exists, choose the sentence boundary in `[lower, hard]` minimizing the same distance;
   - if none exists, choose `target`.
   - equal-distance ties choose the earlier boundary.
8. Emit the exact normalized character slice corresponding to body token offsets `[s,e)`, preceded by the repeated prefix. Do not decode token IDs back into text.
9. If `e=N`, stop. Otherwise set `next_s=max(s+1, e-48)`. Thus overlap is measured only on body tokens, never includes the repeated prefix, never exceeds 48, and always makes progress.
10. Ordinals start at 1 and are formatted as four decimal digits. Empty normalized bodies emit zero chunks. No chunk may exceed 384 pinned-tokenizer tokens.

The implementation records `boundary_kind` (`document_end`, `paragraph`, `sentence`, or `token_fallback`), body token start/end, body overlap count, prefix token count, and `prefix_truncated` in chunk metadata so a manifest can be audited without re-running the splitter.

### 5.3 `filing_v2`

The Core milestone supports 8-K items and EX-99.x exhibits. Section boundaries precede token windows. Chunks never cross unrelated sections. Maximum, target, and overlap are 384/320/48 tokens. Parsing failure emits bounded `section_key=unknown_000` chunks with a degraded flag; it never embeds the entire filing as one fallback vector.

Section detection is deterministic:

- if the canonical filing already supplies ordered sections, preserve that order and normalized `section_key` values;
- otherwise an 8-K body starts a section only at a line matching `(?im)^\s*item\s+(\d{1,2}\.\d{2})\b`; normalize the key to lowercase `item_<number>` (for example `item_1.01`); text before the first match is `unknown_000`;
- an EX-99.x document uses one section key derived from its declared document type, lowercase with punctuation normalized to underscores (for example `ex_99_1`); its paragraph/sentence boundaries are handled by the shared window algorithm;
- duplicate item headings start a new segment and receive deterministic suffixes `_02`, `_03`, and so on in encounter order;
- if no supported heading is found in an 8-K, the full normalized body is section `unknown_000`, is windowed by the shared algorithm, and every resulting chunk carries `section_parse_degraded=true`;
- empty sections emit no chunks; chunks never combine two section keys; the normalized section heading is the repeated prefix capped at 64 tokens.

### 5.4 Stable identities

```text
document_id = provider-native canonical identity

chunk_id = document_id + ":" + chunk_profile_version + ":" +
           section_key + ":" + zero_padded_ordinal

content_hash = SHA256(exact_normalized_embedding_text_utf8)

metadata_hash = SHA256(canonical_json({
  document_id, ticker_associations, available_at, source_class,
  dedup_cluster_id, representative_document_id, eligibility,
  chunk_profile_version
}))
```

Presentation-only image/logo changes do not alter `content_hash`. Ticker, cutoff timestamp, source class, cluster, representative, or eligibility changes alter `metadata_hash` and update retrieval metadata without re-embedding when text is unchanged.

### 5.5 Corpus manifest

```text
manifest_id = SHA256(canonical_json({
  normalization_version,
  chunk_profile_versions,
  source_classifier_version,
  certified_snapshot_identity,
  sorted_active_chunk_inventory,
  tokenizer_revision,
  embedding_revision_or_null
}))
```

`created_at` is reported but excluded from `manifest_id`. An interrupted reconciliation never publishes a current manifest. Removed, ineligible, superseded-profile, and disappeared-child chunks receive tombstones.

## 6. B4 — Cutoff-safe lexical retrieval and Eval Foundation

### 6.1 Eval Foundation timing

Before persisting any comparison pool, B4 creates the agent-agnostic `BenchmarkCase`, evidence-judgment, pool-manifest, lineage-validation, and dataset-version schemas in `catalyst-eval`. No labels are authored in B4.

### 6.2 Eligibility before scoring

Every retrieval mode applies the same predicate before ranking:

```text
active = true
AND manifest_id = requested_manifest_id
AND available_at <= cutoff
AND ticker/evidence/source filters match
```

Filtering top-K after scoring is prohibited because post-cutoff documents could displace legal candidates. Close-to-close cutoffs use the official exchange close for that session, including early-close sessions, converted to UTC. Intraday analysis uses an explicit UTC `as_of` instant.

### 6.3 FTS5/BM25 trace contract

SQLite FTS5 `bm25()` is treated as an engine-specific raw score whose lower value ranks first. Catalyst persists `lexical_raw_score` and one-based `lexical_rank`; it does not compare the raw value numerically with dense or reranker scores.

Default lexical candidate depth is 20 and final display depth is 8. Stable ties use `chunk_id ASC`.

### 6.4 RetrievalResult

Each result retains `chunk_id`, `document_id`, `available_at`, cutoff, filters, source class, raw per-stage scores, one-based per-stage ranks, corpus manifest ID, index manifest ID when applicable, degraded/fallback state, and timing.

## 7. B5 — Attribution workflow and runtime assurance

### 7.1 Deterministic Context Builder

For close-to-close analysis:

```text
r_target,t = (C_target,t / C_target,t-1 - 1) * 100

volume_ratio = V_target,t /
               median(valid volumes in the 20 expected exchange sessions before t)
```

The lookback is exactly the previous 20 expected exchange sessions ending at `t-1`; it never expands farther back to obtain more observations. A prior volume is valid when it is finite, non-null, and strictly positive. If 10–20 valid prior volumes exist inside that fixed window, use the median of all valid values. With fewer than 10, a non-positive median, or missing/non-positive `V_target,t`, emit `volume_context_available=false`, `volume_ratio=null`, and a specific data-quality reason. The artifact records all 20 expected session identities, the valid-session subset, and the denominator used.

The additive descriptive decomposition is:

```text
market_component = r_market
sector_excess = r_sector - r_market
company_specific = r_target - r_sector

r_target = market_component + sector_excess + company_specific
```

These are descriptive return components, never causal contributions.

```text
peer_median_return = median(valid same-session peer returns)
target_vs_peers = r_target - peer_median_return
```

Missing peer data is `not_available`, never zero. Context Builder emits inputs, formulas, outputs, session identities, data-quality flags, and cutoff as a deterministic artifact.

### 7.2 Hypothesis prerequisites

- market: corresponding benchmark OHLCV exists;
- sector: corresponding sector ETF OHLCV exists;
- earnings/guidance, product/demand, legal/regulatory, or macro: matching pre-cutoff evidence or an explicitly modeled event exists;
- peer/supply-chain propagation: a versioned relationship edge, same-session peer context, and pre-cutoff evidence describing the peer event all exist;
- mixed: at least two non-`unexplained` hypotheses independently pass their gates;
- unexplained: context is sufficient but no supported explanation survives;
- abstain: target/context quality is inadequate, zero graded evidence exists, or all explanation gates fail.

An edge plus co-movement is never sufficient. Direction is never inferred from an edge alone.

### 7.3 Ranking without fake confidence

Finalizer uses deterministic lexicographic ordering, not an untrained weighted confidence score:

1. prerequisite gate passed;
2. direct pre-cutoff support exists;
3. number of independent supporting dedup clusters, capped at two;
4. maximum Critic relevance among supporting chunks;
5. fewer source-support degradation flags;
6. lower maximum counter-evidence relevance;
7. novel before previously-known evidence;
8. stable hypothesis enum tie-breaker.

The output may report evidence strength classes and degradation flags, but no self-reported probability or global causal-confidence percentage.

### 7.4 Output states

- `SUFFICIENT`: at least one gate-passed hypothesis, all citations resolve and were Judge-visible, no cutoff violation, and no material coverage degradation;
- `PARTIAL`: supported output exists but coverage is partial/degraded or support is limited to opinion, unknown-origin aggregation, or uncorroborated issuer claims;
- `ABSTAIN`: missing target OHLCV, failed context quality, zero usable evidence, or all gates failed;
- `SYSTEM_ERROR`: schema, model, database, provider, timeout, or runtime failure prevented a valid decision.

Validator may make at most one exceptional repair call. Normal execution remains Critic + Judge. Runtime assurance is deterministic and always records cutoff, citation resolution, visibility, gate validity, trace completeness, identities, budget/retry/repair state, and degraded coverage.

## 8. B6 — Dense retrieval and reranker

### 8.1 Dense similarity

The exact BGE-M3 Hugging Face revision is pinned during the first authorized server build. Output dimension is 1024. If vectors are normalized:

```text
dense_score(q, d) = q dot d
```

Otherwise:

```text
cosine(q, d) = (q dot d) / (norm(q) * norm(d))
```

The manifest records model revision, tokenizer revision, normalization mode, dtype, dimension, corpus manifest ID, and artifact hashes.

### 8.2 Reciprocal Rank Fusion

Using one-based ranks and `k=60`:

```text
RRF(d) = sum over m in {lexical, dense} of 1 / (60 + rank_m(d))
```

Each arm contributes top-20 after identical cutoff and metadata filters. RRF operates on their union and emits fused top-20. Stable ties use best contributing rank, then `chunk_id ASC`.

### 8.3 Reranker candidate preservation

Let `C` be the fused top-20:

```text
set(reranker_input) = set(C)
set(reranker_output) = set(C)
```

The reranker only reorders C and returns the top-8 presentation slice. Timeout or model failure records explicit fallback to RRF ordering. It never retrieves new candidates or silently drops candidates.

### 8.4 Retrieval modes and fallback semantics

The mode names are binding:

| `mode_requested` | Required stages | Successful `mode_served` |
|---|---|---|
| `fts5` | cutoff/filter → FTS5 | `fts5` |
| `dense` | cutoff/filter → dense | `dense` |
| `hybrid` | FTS5 + dense → RRF | `hybrid` |
| `reranked` | FTS5 + dense → RRF → reranker | `reranked` |

`hybrid` never calls the reranker. `reranked` never retrieves a candidate outside fused C. If one retrieval arm fails, requested `hybrid` or `reranked` serves the surviving arm directly (`mode_served=fts5|dense`) and records the failed arm; it does not label one-arm output as hybrid and does not invoke the reranker. If both retrieval arms succeed but the reranker times out/fails, `mode_requested=reranked`, `mode_served=hybrid`, and final ordering is the exact RRF ordering. If both retrieval arms fail, `mode_served=failed`, no result list is returned, and the typed retrieval error is persisted. Every result carries both mode fields plus ordered degradation/fallback reasons.

### 8.5 Retrieval-arm output artifact

B6 adds no SQLite migration. It persists one atomic, untracked JSON artifact per benchmark case and retrieval run:

```text
data/retrieval_arm_outputs/<run_id>/<case_id>.json
```

The writer creates a temporary sibling file, fsyncs it, then renames it. Schema version `1.0.0` is:

```json
{
  "schema_version": "1.0.0",
  "artifact_id": "sha256-hex",
  "run_id": "retrieval-run-id",
  "case_id": "B001",
  "query_sha256": "sha256-hex",
  "cutoff_ts": "2026-01-15T21:00:00Z",
  "filters": {
    "ticker": "AAPL",
    "evidence_types": [],
    "source_classes": [],
    "corpus_manifest_id": "corpus-v1",
    "index_manifest_id": "index-v1"
  },
  "retrieval_config": {
    "lexical_top_k": 20,
    "dense_top_k": 20,
    "fusion_k": 60,
    "fused_top_k": 20,
    "display_top_k": 8,
    "embedding_revision": "40-hex",
    "reranker_revision": "40-hex"
  },
  "arms": {
    "fts5": {"mode_requested": "fts5", "mode_served": "fts5", "status": "ok", "latency_ms": 0.0, "degradation_reasons": [], "results": []},
    "dense": {"mode_requested": "dense", "mode_served": "dense", "status": "ok", "latency_ms": 0.0, "degradation_reasons": [], "results": []},
    "hybrid": {"mode_requested": "hybrid", "mode_served": "hybrid", "status": "ok", "latency_ms": 0.0, "degradation_reasons": [], "results": []},
    "reranked": {"mode_requested": "reranked", "mode_served": "reranked", "status": "ok", "latency_ms": 0.0, "degradation_reasons": [], "results": []}
  },
  "created_at": "ISO-8601 UTC"
}
```

Each result object contains `chunk_id`, `document_id`, `available_at`, `source_class`, one-based `rank`, and nullable `lexical_raw_score`, `lexical_rank`, `dense_score`, `dense_rank`, `fusion_score`, `fusion_rank`, `reranker_score`, and `reranker_rank`. Result arrays are stored in served order and have unique chunk IDs.

`artifact_id` is SHA-256 of canonical JSON containing every field above except `artifact_id`, `created_at`, and all `latency_ms` values. Therefore identical retrieval identities, configuration, degradation state, scores, ranks, and ordered results produce the same artifact ID despite runtime timing differences. The union judgment pool is derived only from these persisted arm arrays and records the source `artifact_id`; it is not generated from ephemeral in-memory results. B7 reads these JSON files as ResultPack inputs. Local arm-output files, union pools, and result packs remain untracked.

B6 persists FTS5, dense, hybrid, and reranked outputs through this artifact and creates the union judgment pool. It does not make the final keep/kill decision before B7 grading.

## 9. B7 — API, compact evaluation, and backend release

### 9.1 Benchmark counts

Exactly 12 Core records:

- 6 `core_answerable`;
- 2 `core_abstain`;
- 4 `retrieval_only`.

Retrieval comparisons use 10 cases: 6 answerable + 4 retrieval-only. Workflow evaluation uses 8 cases: 6 answerable + 2 abstain. Legacy 65-case fixtures are not headline labels.

### 9.2 Retrieval metrics

Relevant evidence means human grade 1 or 2.

```text
Recall@8 = |relevant_chunks intersect top8| / |relevant_chunks|

DCG@8 = sum(i=1..8) ((2^rel_i - 1) / log2(i + 1))

nDCG@8 = DCG@8 / IDCG@8
```

Metric functions receive ordered `top_ids` plus `judgments_by_chunk_id`; chunk IDs never encode grades. A judgment is an explicit record with `chunk_id`, grade 2/1/0, rationale, annotator, and timestamp. Missing IDs are `UNJUDGED`, never grade 0.

All retrieval metric functions return a typed `MetricValue` containing `value: float|int|None`, `status: OK|NOT_APPLICABLE|NEEDS_JUDGMENT`, numerator, denominator, and `unjudged_ids`. Any unjudged chunk inside the evaluated top-K yields `NEEDS_JUDGMENT` and `value=null`; no point estimate is emitted. If `unjudged@8 > 2`, the pool must additionally be extended before grading. No decisive comparison proceeds until every top-8 item for both compared arms is judged.

`relevant_chunks` is the set of all explicitly judged grade-1/2 chunks in the certified union pool for the case. If it is empty, Recall@8 is `NOT_APPLICABLE`, not zero. `IDCG@8` is computed by sorting all explicitly judged pool grades descending, taking the first eight, and applying the DCG formula. If `IDCG@8=0`, nDCG@8 is `NOT_APPLICABLE`. Stable ordering for equal grades uses `chunk_id ASC` only to make the ideal list reproducible; equal grades contribute the same gain.

`primary_hit@8` is 1 when any grade-2 chunk appears in top-8. Its aggregate denominator contains only retrieval cases with at least one grade-2 chunk in the certified corpus; corpus-coverage failures are reported separately.

Reranker comparison requires candidate-set preservation and reports per-case delta nDCG@8, delta primary-hit@8, improved/regressed case IDs, median/max latency, timeout/fallback count, and named-case evidence tables. A one-case trade-off defaults to the cheaper configuration.

### 9.3 Workflow metrics

The eight workflow cases use a 2x2 expected answer/abstain versus actual answer/abstain confusion matrix.

```text
abstain_reason_match =
correct stated abstention reason class /
expected-abstain cases
```

No composite score, self-reported confidence calibration, prose similarity, MRR, Precision@K, p95 latency, or generalized attribution-accuracy claim is permitted.

### 9.4 API contract groups

- Data: update preview, expected plan hash, confirmation, capability report, run status, cancel, rerun/resume, corpus/index identities;
- News: stable article ID, title, description, publisher, source class, `available_at`, image URL, original URL, ticker links, provenance summary;
- Chart: OHLCV candles, session calendar, market/sector/peer context, freshness and missing-data flags;
- Attribution: request identity, cutoff, ranked hypotheses, supporting/counter-evidence, unavailable evidence, output status, run assurance;
- Trace: retrieval stages, node events, decisions, model/prompt/manifest identities, latency, tokens, cost, degraded/fallback state;
- Evaluation files: BenchmarkCases, MetricRecords, ResultPack, generated Markdown scorecard, zero-network replay gate.

Provider image URLs are presentation metadata and excluded from embedding text. The local UI opens the original article URL directly with safe external-link attributes; Catalyst does not proxy or cache licensed media in the Core milestone.

#### Common HTTP rules

- All response models use `extra="forbid"` and carry `schema_version="1.0.0"` at the top level.
- Domain errors use `{ "code": str, "message": str, "details": object|null }`.
- HTTP mapping is fixed: validation 422; malformed/unsupported request 400; missing resource 404; stale plan, writer conflict, or illegal transition 409; unavailable dependency 503; unexpected sanitized failure 500.
- Dates are ISO `YYYY-MM-DD`; timestamps are UTC ISO-8601 with `Z`. Range filters are `[from_ts, to_ts)` (inclusive start, exclusive end).
- Cursor lists use URL-safe base64 encoding of canonical JSON containing the final sort keys. Invalid or stale cursors return 400. Responses contain `next_cursor: str|null`.
- List ordering and tie-breakers below are mandatory. No endpoint returns embedding text, raw provider payloads, secrets, private model configuration, or local filesystem paths.

#### Data/update routes

| Method and path | Request | Success | Required response fields |
|---|---|---|---|
| `POST /api/updates/preview` | tickers, sources, start_date, end_date, `allow_stale_ohlcv=false` | 200 | schema_version, plan_hash, requested window/universe, stage order, exact cells-by-stage, request/page caps, warnings, capability snapshot |
| `POST /api/updates/execute` | `expected_plan_hash` and the same request fields | 202 | schema_version, run_id, status=`PLANNED`, expected_plan_hash |
| `GET /api/updates/{run_id}` | none | 200 | run identity/status/stage, counts, partial/degraded flags, plan hashes, timestamps, lease/cancel state, report identity |
| `POST /api/updates/{run_id}/cancel` | none | 202 | run_id, `cancel_requested=true`, current status |
| `POST /api/updates/{run_id}/resume` | expected_plan_hash | 202 | parent_run_id, new run_id, status=`PLANNED`, expected_plan_hash |
| `GET /api/capabilities` | none | 200 | provider/profile availability without secrets, supported sources/tickers, server-only capabilities |
| `GET /api/data/identities` | none | 200 | SQLite user_version, latest successful update run ID, corpus manifest ID, index manifest ID, retrieval profile versions |

Preview is structurally zero-write and zero-network. Execute re-plans and returns 409 `plan_drift` before starting a run when the expected hash differs. A live writer lease returns 409 `writer_conflict`. Cancel is cooperative and idempotent. Resume creates a new run linked to the parent; it never rewinds a terminal row.

#### News routes

`GET /api/news/{ticker}` accepts required `from_ts`, required `to_ts`, `limit` default 20/max 100, and optional cursor. It orders by `available_at DESC, article_id ASC`. Each item contains `article_id`, title, description, publisher, source_class, available_at, image_url, original_url, ticker_links, provenance_summary, and coverage_degraded. The cursor encodes the last `(available_at, article_id)`.

`GET /api/news/articles/{article_id}` returns the same fields plus complete normalized provenance summaries. Missing article is 404. The ticker route and article route must not shadow one another. Image/original URLs may be null; internal embedding text is never exposed.

#### Chart/context routes

`GET /api/ohlcv/{ticker}` accepts required `start_date`, required `end_date`, with an inclusive trading-date range and maximum 2,000 sessions. It orders candles `date ASC`. Response fields are schema_version, symbol, candles, count, source identities, `latest_available_session`, `requested_through_session`, `freshness_lag_sessions`, missing_session_dates, and coverage_degraded. Unknown ticker is 404; a valid ticker with no rows returns 200 with an empty candle list and explicit coverage flags.

`GET /api/session/{ticker}?trade_date=...` returns target candle/previous close/return/volume context, market/sector/peer context from B5, expected exchange-close cutoff, formula inputs, context availability reasons, and corpus/index identities. A non-trading date returns 200 with `is_trading_day=false`; malformed date is 422.

#### Attribution and trace routes

`POST /api/live-runs` remains the attribution submission route and returns 202 semantics through its typed body: run_id, status, ticker, trade_date, cutoff_ts, model identity without credentials, corpus_manifest_id, index_manifest_id, prompt identities, and trace_schema_version. `GET /api/live-runs/{run_id}` returns the same identities plus output status, ranked hypotheses, assurance summary, timing, token/cost status, and sanitized failure.

`GET /api/live-runs/{run_id}/workspace` is the complete UI projection: context artifact, Judge-visible evidence in served order, supporting/counter-evidence links, unavailable evidence, ranked hypotheses, output status, per-run assurance, and degradation flags. Citation/evidence IDs must resolve within that response.

`GET /api/live-runs/{run_id}/events?after_seq=N` orders by `event_seq ASC`, returns only events with `event_seq>N`, and serializes node/status/model/prompt/timing/token/cost/error fields plus trace schema version. `GET /api/live-runs/{run_id}/artifacts` accepts optional event_seq and declared artifact_type, orders by `(event_seq, artifact_type, created_at)`, and returns parsed JSON payloads under a versioned envelope. Unknown run is 404; an existing run with no events/artifacts returns an empty list. Invalid artifact type is 422.

### 9.5 Keep/kill and release

Components are retained only with named-case benefit or required reliability value. Reranker implementation and measurement are mandatory; retaining it is not. Backend release requires all package suites, synthetic fixture quickstarts, provenance/cutoff/assurance landmines, ResultPack validation, and zero-network replay to pass.

## 10. Required implementation-plan structure

Each B2–B7 plan must:

1. distinguish current, partial, and proposed behavior by inspecting code;
2. list exact touched/new files and migration ownership;
3. use TDD with non-tautological landmine tests;
4. run focused tests before `.venv/bin/python -m pytest packages/<pkg> -q`;
5. preserve both database SHAs during planning and tests that do not explicitly operate on disposable copies;
6. isolate supervised provider calls and GPU work behind explicit operator authorization;
7. keep provider probes, raw licensed payloads, embeddings, secrets, and local result artifacts untracked;
8. define evidence-report output and independent reviewer checks;
9. avoid stage, commit, or push unless separately authorized;
10. treat this contract as authoritative when older plans or research notes disagree.
