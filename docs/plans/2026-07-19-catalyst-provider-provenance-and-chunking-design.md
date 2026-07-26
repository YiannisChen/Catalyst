# Catalyst Provider Provenance and Chunking Design

- Date: 2026-07-19
- Status: final and ratified subordinate design (amended 2026-07-21: source/evidence classification §8.1, dedup/novelty field separation §8.2, peer-news request semantics §5.1–§5.2, FRED derived-series note §5.5, landmine tests 25–28)
- Parent: `docs/plans/2026-07-19-catalyst-open-source-workbench-design.md`
- Technical authority: `docs/plans/2026-07-21-b2-b7-technical-contracts.md` for formulas, migration ownership, and B2–B7 sequencing
- Scope: request/page provenance, raw-payload retention, provider-specific normalization, chunk profiles, corpus identity, and incremental re-indexing
- Discipline: design only; no provider calls, database writes, staging, or commits

The ratified review is integrated with one scope correction: raw payload rows remain request-scoped rather than shared across identical responses. This avoids nullable cell metadata and cross-request deletion/retention ambiguity at negligible storage cost.

## 1. Binding decision

Catalyst uses four distinct identities and never conflates them:

1. **Fetch cell:** the logical work unit from an update plan, such as Polygon news for NVDA on one calendar date.
2. **Provider request attempt:** one real HTTP attempt, including retries and pagination requests.
3. **Raw response payload:** the exact response body returned by one successful or forensically retained HTTP attempt/page.
4. **Canonical document and retrieval chunk:** normalized business records and the source-specific text units derived from them.

For a Polygon response containing 30 articles, the required result is:

```text
1 fetch cell
1 provider request attempt (when no retry/pagination)
1 append-only raw response payload
30 canonical article rows
30 primary news chunks in the usual short-news case
```

The raw response remains intact for replay and audit. Its `results[]` items are normalized separately. Raw response batches are never embedded and never treated as retrieval documents.

For pagination, each `next_url` request is a distinct request attempt and each page is a distinct raw payload. Catalyst does not synthesize several pages into one object and call that object “raw.”

## 2. Verified current provider shapes

Read-only inspection of the current Dev DB established these shapes:

| Source | Current raw rows | Actual raw shape | Canonical action | Retrieval action |
|---|---:|---|---|---|
| Polygon News | 3,516 | object containing `news.results[]`; current connector requests one day with `limit=50` | one `articles` row per result plus `article_tickers` associations | one short-news chunk normally; token windows only for long text |
| Finnhub Company News | 420 | top-level article list | one `articles` row per result | same news profile as Polygon |
| Polygon OHLCV | 3,349 | object containing one daily bar in `ohlcv.results[]` | one structured OHLCV row | never embedded; consumed by Context Builder |
| FMP Fundamentals | 3,500 | object with income, balance-sheet, and cash-flow lists, five periods each | content-addressed snapshot plus structured statement/period rows | excluded from v1 retrieval until an event/statement materializer is approved |
| FRED Macro | 11 | one series payload with `observations[]` ranging from 13 to 1,278 items | one structured observation per date/release identity | retrieval uses release/event summaries, never the whole observation history |
| SEC Submissions | 3 | company metadata plus filing history arrays | filing metadata rows | submissions batch is not embedded |
| SEC Primary Document | 9 | HTML/text document payload | filing document with extracted sections | section-aware token chunks; never one whole-document embedding |

Current news length distribution confirms that source-specific behavior is necessary:

- Polygon: 13,920 articles, average description about 340 characters, maximum 758, zero descriptions at or above 800 characters.
- Finnhub: 22,941 articles, average description about 216 characters, maximum 2,950, only 86 descriptions at or above 800 characters.

Therefore the normal news unit is one article, not a generic fixed-size split. Long-document rules primarily matter for filings and a small minority of news records.

## 3. Current provenance defect

The repository records some operational state but does not preserve a complete request ledger:

- `ingestion_runs` records a run summary and progress.
- `source_checkpoints` records one logical cell result per `(run_id, source_type, ticker, date)`.
- `raw_assets` stores compressed payloads.
- `compute_asset_id(ticker, date, source_type, data_version)` identifies a logical cell, not a unique HTTP response.
- `upsert_raw_asset()` uses `INSERT OR REPLACE`, so rerunning a cell can overwrite the previous payload and `fetched_at`.
- retries and pagination attempts are summarized into checkpoint fields instead of being individually auditable.
- current Dev DB history does not populate `raw_asset_id`; the current OHLCV execution path populates it for new writes, while news history and news request attempts still lack complete lineage.
- current orchestration serializes validated endpoint data into a generated wrapper before raw storage; this is not guaranteed to be the exact transport response body.

The existing DDL comment that raw assets are immutable is therefore not true operationally. The implementation must fix the behavior rather than repeat the comment.

## 4. Final request-provenance model

### 4.1 Existing run and checkpoint roles

Retain:

- `ingestion_runs` as the operator-visible run aggregate;
- `source_checkpoints` as the latest result of one planned fetch cell within a run.

They are not sufficient as the request history.

### 4.2 New append-only request ledger

Add `provider_request_attempts`:

```text
request_id                 TEXT PRIMARY KEY
run_id                     TEXT NOT NULL
logical_fetch_id           TEXT NOT NULL
source_type                TEXT NOT NULL
provider                   TEXT NOT NULL
endpoint_name              TEXT NOT NULL
ticker_or_series           TEXT
window_start               TEXT
window_end                 TEXT
attempt_no                 INTEGER NOT NULL
page_no                    INTEGER NOT NULL DEFAULT 0
parent_request_id          TEXT
request_fingerprint        TEXT NOT NULL
request_params_redacted    TEXT NOT NULL
cursor_fingerprint         TEXT
started_at                 TEXT NOT NULL
completed_at               TEXT
status                     TEXT NOT NULL
http_status                INTEGER
latency_ms                 REAL
items_count                INTEGER
retry_after_seconds        REAL
rate_limit_remaining       INTEGER
provider_request_id        TEXT
error_class                TEXT
error_message_redacted     TEXT
raw_asset_id               TEXT
response_sha256            TEXT
response_bytes             INTEGER
```

Rules:

- one row per actual attempt, including failed retries and pagination pages;
- append-only after completion except the single running-to-terminal state transition;
- request fingerprint includes method, host, path, sorted non-secret parameters, body hash, and provider profile version;
- API keys, authorization headers, cookies, and full cursor URLs are never persisted or logged;
- pagination cursor is stored only as a hash unless the provider contract proves it contains no credential or personal data;
- response/rate-limit headers use an allowlist;
- failed AUTH, RATE_LIMIT, TRANSPORT, TIMEOUT, PARSE, and EMPTY_VALID attempts remain distinguishable.
- connector adapters redact `apiKey`, authorization headers, cookies, signed query parameters, error text, and `next_url` before any request object can reach logs or the ledger; ledger-time redaction alone is too late.

Extend `source_checkpoints` with a `logical_fetch_id`, `request_count`, `pages_received`, `items_received`, and `is_complete`. It remains the logical-fetch aggregate; no additional generic workflow table is required for the 60% milestone. Any ordered response-manifest hash is derived from request-attempt rows and is not duplicated as mutable checkpoint state.

### 4.3 Raw payload identity

Continue using `raw_assets` for payload bytes, but change new-write semantics:

- new response identity is request-attempt/page-specific, not `(ticker, date, source)`-specific;
- recommended ID: `raw:{request_id}`; `request_id` already identifies the attempt/page;
- add `response_sha256`, `request_id`, `page_no`, and `content_encoding` columns through an additive migration;
- replace `INSERT OR REPLACE` with an append-only insert;
- an existing identical ID/hash is an idempotent no-op;
- an existing ID with different bytes is an integrity error;
- old deterministic asset IDs remain readable as legacy payloads and are never bulk rewritten.

`source_checkpoints.raw_asset_id` remains a compatibility pointer for single-payload cells. The authoritative one-to-many relationship is `provider_request_attempts`, because a cell may have retries and pages.

New request-scoped raw rows keep `ticker` and `reference_date` non-null. Catalyst intentionally does not deduplicate identical response bytes across requests in the 60% milestone: empty or repeated responses are small, while one-row-per-response makes lineage, retention, and deletion semantics unambiguous. `response_sha256` supports integrity checks and later storage analysis, not cross-request identity. Content-addressed payload deduplication may re-enter only if measured storage growth justifies it.

Add store-level triggers rejecting UPDATE or DELETE of v2/request-scoped raw rows. Append-only behavior is enforced by SQLite, not only by writer convention. A future provider-mandated retention deletion requires an explicit maintenance migration rather than an ordinary application write.

Fetch adapters must expose `resp.content` before JSON validation. The stored payload is the HTTP client-decoded body; record the wire `Content-Encoding` header separately and label the stored representation as decoded. Generated endpoint wrappers, merged page envelopes, and canonicalized JSON are derived artifacts and cannot be stored as the only raw payload.

### 4.4 Normalized provenance

Add an append-only `normalized_provenance` table:

```text
entity_type                TEXT NOT NULL
entity_id                  TEXT NOT NULL
entity_version             TEXT NOT NULL
raw_asset_id               TEXT NOT NULL
normalizer_version         TEXT NOT NULL
created_at                 TEXT NOT NULL
PRIMARY KEY (entity_type, entity_id, entity_version, raw_asset_id)
```

This is required because the same article or filing may appear in multiple ticker requests, pagination pages, retries, or provider refreshes. Scalar `articles.raw_asset_id` remains a compatibility pointer and is not the complete lineage record.

### 4.5 Transaction boundary

For a successful page:

1. insert/update the request attempt to terminal success;
2. insert the exact raw payload;
3. normalize its entities idempotently;
4. commit payload, lineage, normalized rows, and cell progress atomically where SQLite transaction scope permits.

Transport failures have no raw payload. Malformed/parse responses may retain raw bytes for forensics. Secrets are redacted before any error text is stored.

## 5. Provider-specific normalization

### 5.1 Polygon News

- Polygon serves both target and peer news cells (the parent design §4.6 binds peer coverage to Polygon only for the Core Exit Gate); peer cells are ordinary fetch cells drawn from the versioned peer/relationship manifest, subject to the same ledger, budget, plan-hash, and rate-limit accounting as target cells — peer coverage is never treated as free;
- follow `next_url` until exhausted, canceled, or a recorded request budget stops the cell;
- each page is a request attempt/raw payload;
- each `results[]` item becomes one globally namespaced `article_id`;
- page overlap is deduplicated by provider-native article ID;
- `article_tickers` records every ticker association rather than duplicating article text;
- URL, image, publisher, timestamps, provider ID, and raw lineage remain metadata;
- a partial pagination stop marks the cell `partial`, never success.
- default safety cap is provider-profile data included in the plan hash; initial Polygon profile is page size 50, maximum 20 pages and 1,000 items per ticker/day cell;
- cursor repetition is a loop error, not successful exhaustion.

### 5.2 Finnhub Company News

- the top-level list is one raw payload;
- each list item becomes one article;
- repeated range requests remain separate request attempts while article upsert is idempotent;
- Finnhub's mostly single-ticker associations are retained honestly; no inferred multi-ticker links are fabricated;
- Finnhub remains **target-only** for the Core Exit Gate (parent design §4.6): 73% of the current Finnhub corpus is Yahoo-publisher aggregated content, so peer expansion would roughly double Finnhub request cells for mostly duplicative `aggregated_unknown`-class material. Finnhub peer cells are a Showcase re-entry gated on a measured peer-evidence recall gap.

### 5.3 Polygon OHLCV and fallback OHLCV

- one bar becomes one structured record;
- primary/fallback requests each receive their own request-attempt record;
- source precedence applies at normalized upsert time;
- raw responses are never text chunks.

### 5.4 FMP Fundamentals

- stop treating each trading date as a distinct logical fundamentals snapshot when payload content is unchanged;
- identify a snapshot by ticker, provider endpoint versions, reported periods, fetch time, and response hash;
- normalize statements and periods for structured comparison;
- do not embed the merged raw statements in v1;
- a future deterministic earnings/event materializer may emit compact textual evidence with its own version.

### 5.5 FRED

- fetched series and derived series are distinct classes: the source map lists the 11 fetched series; `T10Y2Y` is derived at normalization time as DGS10 − DGS2 (released at the max of its components) and is certified alongside fetched series — its absence from the fetched-series map is by design, not an omission;
- preserve one raw series response per request;
- normalize every observation with series, observation date, realtime/release bounds, value, and raw lineage;
- historical observation arrays are not retrieval chunks;
- no FRED text enters the 60% Retrieval Corpus; structured observations feed Context Builder. A later versioned release/event materializer is the only re-entry path.

### 5.6 SEC

- submissions payload normalizes filing metadata and is never embedded;
- each primary document/exhibit response is separately retained;
- extracted document sections carry filing ID, accession, document type, accepted/filed timestamps, source URL, and raw lineage;
- retrieval chunks derive only from extracted document sections.
- primary documents and exhibits are separate canonical documents; do not select exactly one by alphabetical document-type order. Deduplicate identical extracted text by content hash.

## 6. Chunking principles

1. Chunk only canonical, eligible documents. Never chunk raw payload JSON, request envelopes, images, URLs, or operational metadata.
2. Normalize and deduplicate before chunking.
3. Use semantic document boundaries first and token windows second.
4. Use one searchable representation at a given granularity; do not embed both a long parent and all of its children, which causes duplicate retrieval crowding.
5. Every chunk inherits parent identity, ticker associations, source/provider, `available_at`, URL metadata, source tier, and dedup cluster.
6. Every chunking rule is versioned and included in the corpus/index manifest.
7. Token counts use the pinned embedding tokenizer/revision, not character counts.

## 7. Binding chunk profiles

### 7.1 News profile `news_v2`

Canonical text input:

```text
title + newline + description
```

Rules:

- the v2 default maximum is 384 tokens including repeated context. This is a retrieval-granularity and citation-precision choice, not a claim about the embedding model's maximum context. The final implementation also asserts compatibility with the pinned reranker revision;
- if normalized text is at most 384 tokens: emit exactly one searchable chunk;
- if longer: split on paragraph/sentence boundaries into target 320-token chunks with up to 48-token overlap;
- prepend the title to child chunks when it is not already present, while counting the prefix against the token budget;
- do not embed a duplicate whole-article parent when child chunks exist;
- for a single-chunk article, the canonical `articles` row is the parent; do not create a redundant non-searchable parent chunk record. Multi-chunk articles retain only their canonical article parent plus searchable children;
- URLs, publisher logos, images, and ticker JSON remain metadata only.

Rationale: almost every current Polygon/Finnhub article fits one chunk. Article-level chunks preserve event context and exact citation identity. Token windows are only a safety path for unusually long descriptions or future full-text providers.

### 7.2 Filing profile `filing_v2`

- scope deterministic section parsing to currently ingested 8-K material: detect `Item N.NN` boundaries in primary documents; use heading/paragraph mode for EX-99.x exhibits;
- preserve section title and hierarchy;
- use the same pinned effective retrieval budget: maximum 384 tokens, target about 320 tokens, and up to 48-token overlap inside the same section;
- never overlap across unrelated filing sections;
- prepend compact filing/form/section context;
- do not embed the entire filing as an L1 vector;
- retain a non-searchable parent document record;
- index primary documents and relevant exhibits as separate canonical documents; use a versioned document-selection policy and content-hash deduplication, never alphabetical winner selection;
- when section parsing fails, emit bounded chunks with `section_key=unknown` and a degraded extraction flag rather than embedding the whole document.
- generic 10-K/10-Q parsing is outside the 60% milestone and re-enters only after those forms are actually ingested.

Rationale: current whole-filing L1 can exceed embedding limits and mix unrelated subjects; current one-sentence L2 fragments lose the company, period, and section context.

### 7.3 Macro profile

No FRED text is embedded in the 60% milestone. Structured observations feed Context Builder. A future `macro_release_v1` materializer must produce one deterministic, versioned release/event summary rather than chunking raw observation arrays.

### 7.4 Fundamentals profile

No FMP fundamentals text is embedded in the 60% milestone. Structured values feed Context Builder or a future versioned earnings-event materializer. This avoids retrieving stale, duplicated, unexplained numerical tables.

### 7.5 OHLCV profile

No OHLCV text chunks. Prices and volumes are structured Context Builder inputs.

## 8. Stable identities and manifests

Document identity and chunk identity are separate:

```text
document_id = provider-native canonical identity
chunk_id = document_id + chunk_profile_version + section_key + zero-padded ordinal
content_hash = SHA-256 of exact normalized embedding text
```

Rules:

- text change with unchanged boundaries updates `content_hash` and marks the chunk stale for re-embedding;
- boundary/profile change creates new chunk IDs because the profile version changes;
- deleted/ineligible documents tombstone their chunks;
- metadata-only changes such as image URL do not trigger embedding;
- changes to ticker associations, cutoff timestamps, source tier, or eligibility update index metadata even when the vector is reusable.

The corpus manifest records:

```text
normalization_version
chunk_profile versions by source kind
source_classifier_version
embedding model and exact revision
tokenizer and exact revision
corpus database identity / certified snapshot identity
document count
searchable chunk count by source/profile
content-hash inventory hash
created_at
```

### 8.1 Source/evidence classification (amended 2026-07-21, binding)

Every canonical document and chunk carries a `source_class` drawn from exactly seven values, separating **data role** from publisher prestige:

| Class | Members (current corpus) | Evidentiary scope |
|---|---|---|
| `structured_market_data` | Polygon/yfinance OHLCV | deterministic price/volume calculations only; can never establish an event narrative or causal explanation |
| `official_government` | SEC filings/sections; FRED observations | official filing/macro facts |
| `issuer_disclosure` | issuer-hosted filings; future direct IR releases | proves the issuer disclosed something; issuer claims stay issuer claims unless independently corroborated |
| `corporate_press_release` | GlobeNewswire; identified PR Newswire / Business Wire | primary evidence of the announcement; not independent verification of its statements |
| `reported_news` | CNBC, MarketWatch, identifiable original reporting | may support event narratives |
| `analysis_opinion` | The Motley Fool, Seeking Alpha, Zacks, ChartMill, Fintel | may generate and support hypotheses; never presented as independently corroborated when it is the only supporting class |
| `aggregated_unknown` | Yahoo, Finnhub-tagged, unrecognized publishers | originating source unresolved; never silently treated as opinion or official; carries an origin-unknown flag |

Classification algorithm (static, auditable, versioned as `source_classifier_version` in the CorpusManifest; no learned ranking model):

1. source kind first (OHLCV → `structured_market_data`; SEC/FRED → `official_government`; filings materialized from issuer-hosted sources → `issuer_disclosure`);
2. then normalized `article_url` host/origin;
3. then publisher metadata fallback;
4. unresolved → `aggregated_unknown`.

The taxonomy is consumed by exactly three mechanisms and nothing else: evidence-pack dedup representative selection (§8.2); Judge evidence labels; and the deterministic assurance flags `opinion_only_support`, `unknown_origin_support`, and `issuer_claim_only_support`. No source-quality scores and no hardcoded financial truth weights exist anywhere.

### 8.2 Dedup identity, representative selection, and novelty time (amended 2026-07-21, binding)

Three separate fields, never conflated:

```text
dedup_cluster_id
cluster_first_available_at
representative_document_id
```

Rules:

- `cluster_first_available_at` is the minimum valid pre-cutoff `available_at` across cluster members and is the only input to novelty/timeline logic;
- `representative_document_id` is selected independently for presentation and Judge input; preference may consider identifiable origin and source class;
- selecting a later official or better-described representative never rewrites novelty time;
- all cluster members retain full provenance;
- one cluster contributes at most one default evidence-pack item;
- benchmark records retain both the representative and the cluster identity.

## 9. Incremental synchronization

For each certified update:

1. normalize new/changed provider payloads;
2. recompute eligibility and dedup groups;
3. derive chunks using the pinned source profile;
4. compare `(chunk_id, content_hash, metadata_hash)` with `index_state`;
5. embed only new or content-changed chunks;
6. update metadata without re-embedding when only metadata changes;
7. tombstone removed/ineligible chunks;
8. publish a new index manifest only after all writes succeed;
9. expose corpus/index lag and failure state separately.

An interrupted build never publishes a partially complete manifest as current.

Current incremental behavior that consults only selected `pending` rows and does not fully reconcile removed child chunks is non-canonical. The first index rewrite must compare all active chunk versions and emit tombstones for disappeared boundaries; this is mandatory, not an optional optimization.

### 9.1 Mandatory reconciliation cases

The rewrite must handle all of these verified failure modes:

1. content changes to rows already marked `embedded`, not only `pending`;
2. documents that become ineligible or lose canonical status;
3. child chunks that disappear when text shrinks or boundaries change;
4. retirement of the current searchable whole-parent plus searchable-child duplication;
5. splitter/profile changes, with profile version in chunk identity;
6. legacy per-ticker article chunk identities that duplicate one canonical article;
7. filing document-selection changes when a new exhibit arrives;
8. dedup-group reassignment that makes a previously embedded article non-canonical;
9. normalized content changes after raw re-derivation;
10. metadata-only ticker, cutoff, tier, eligibility, or association changes through `metadata_hash` without re-embedding.

## 10. Required landmine tests

1. One Polygon response with 30 articles produces one request attempt, one raw payload, 30 canonical articles, and 30 short-news chunks; no chunk contains two article IDs.
2. Two Polygon pages with one overlapping article produce two request rows, two raw payloads, and one canonical article for the overlap.
3. Retrying the same cell records every attempt and never overwrites prior raw payload bytes.
4. A page-2 failure marks the cell partial and records page 1; it cannot report full success.
5. Request fingerprints and stored error messages contain no API key, authorization header, cookie, or raw secret cursor.
6. Raw JSON containing many news items can never be passed to an embedding builder.
7. Every eligible short article emits exactly one searchable news chunk.
8. A long article respects the token ceiling and overlap ceiling; reconstruction order is deterministic.
9. Child news chunks never omit parent identity, title context, `available_at`, ticker links, or article URL metadata.
10. Image/logo/article URL changes do not alter embedding content hash.
11. A filing larger than the embedding context limit emits no searchable whole-document chunk.
12. Filing chunks never cross section boundaries and carry section identity.
13. FRED observation arrays, FMP raw statements, SEC submissions, and OHLCV payloads emit zero raw-derived text chunks.
14. Changing `news_v2` or `filing_v2` parameters changes chunk profile identity and invalidates dependent index manifests.
15. A metadata-only ticker-association change reuses the vector but updates retrieval filters.
16. Post-cutoff chunks are excluded identically by FTS5, dense, hybrid, reranker input, and Validator.
17. Exact transport bytes survive parsing/re-derivation; a generated endpoint wrapper cannot satisfy the raw-payload assertion.
18. Every canonical entity version joins to at least one raw payload through `normalized_provenance`.
19. Repeated identical response bytes produce distinct request-scoped raw rows with equal `response_sha256`; neither row overwrites the other.
20. Pagination loop, duplicate page, cap exhaustion, and partial normalization each produce distinct, asserted terminal states.
21. UPDATE or DELETE against a v2 request-scoped raw row is rejected by the SQLite store guard.
22. Connector request records, error messages, and cursor fingerprints contain no `apiKey` even when a provider URL contains signed query parameters.
23. A chunk-profile version change tombstones old-profile chunks; one document cannot return active chunks from two profile versions.
24. Reconciliation detects content changes to already embedded rows, removed/ineligible documents, disappeared child chunks, dedup reassignment, and metadata-only ticker changes.
25. Changing a dedup cluster's `representative_document_id` never changes its `cluster_first_available_at`; novelty/timeline outputs are byte-identical before and after a representative change.
26. An unresolvable-origin publisher classifies as `aggregated_unknown` and emits the origin-unknown flag; it is never silently classified as opinion or official evidence.
27. Changing `source_classifier_version` invalidates dependent corpus manifests; two manifests with different classifier versions are distinct identities.
28. A peer fetch cell consumes ledger rows, budget, and plan-hash scope identically to a target cell; no peer request bypasses the request ledger.

## 11. Implementation boundaries

1. **Request ledger migration:** additive schema, append-only raw writes, request redaction/fingerprint utilities, compatibility reads.
2. **Connector instrumentation:** one request-attempt record per attempt/page for Polygon, Finnhub, FMP, FRED, SEC, and OHLCV fallback.
3. **Pagination and partial-cell semantics:** Polygon pagination, page lineage, budget/cancel behavior.
4. **Normalization contracts:** provider-specific entity counts and raw-to-canonical lineage tests.
5. **Chunk profile rewrite:** token-aware news and section-aware filing builders; removal of sentence-only and whole-filing searchable representations.
6. **Manifest/index-state amendment:** profile identities, metadata hash, tombstones, atomic manifest publication.
7. **Certified-fixture migration:** read old raw IDs and index artifacts without rewriting history; new writes use v2 provenance.

No live provider call is part of implementation tests. Sanitized recorded fixtures cover response shapes. One later supervised provider canary verifies request ledger behavior without printing secrets.

Successful pages from a partial logical fetch may be normalized, but the logical fetch remains `partial`, certification records incomplete coverage, and downstream attribution receives a coverage-degraded flag. Partial data can never be silently represented as complete.

## 12. Retention and distribution policy

- raw payloads are local operational artifacts and are never committed to the public repository by default;
- each provider profile declares `retention_mode = full | ttl | metadata_only` according to provider terms;
- the 60% milestone implements `full` and `metadata_only`; `ttl` is a declared future mode and no deletion scheduler/reaper is built until a provider requires it;
- `full` retains exact bytes locally; `metadata_only` stores no body when provider terms prohibit retention;
- sanitized fixture payloads used in tests are separately reviewed and intentionally minimal;
- public corpus/index exports contain only data the project is permitted to redistribute;
- retention policy, not an implementation shortcut, decides whether forensic replay is available for a provider.

## 13. Explicit non-goals

- no storage of secret request headers or API keys;
- no embedding of raw provider JSON;
- no universal character-based splitter;
- no semantic LLM chunker;
- no image OCR or multimodal embedding;
- no full-text news scraping beyond provider-supplied content;
- no rewriting of historical raw IDs or frozen artifacts;
- no separate raw-page table unless the additive request-ledger/raw-assets design proves insufficient in implementation.
