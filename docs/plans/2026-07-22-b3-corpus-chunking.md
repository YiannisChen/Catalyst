# B3 — Corpus and Chunking Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Normalize provider items as independent canonical domain records. Implement `news_v2` and `filing_v2` chunk profiles. Emit tombstones, reconcile corpus changes, and publish versioned corpus manifests atomically.

**Architecture:** Builds on B2's canonical articles and normalized provenance. Adds eligibility rules, chunk profile engines (token-aware with pinned tokenizer), corpus reconciliation, tombstone emission, metadata-only updates, and deterministic CorpusManifest. Uses migration v9 for corpus table extensions.

**Tech Stack:** Python 3.12+, SQLite, pinned BGE-M3 tokenizer, pytest

**Binding Contract:** `docs/plans/2026-07-21-b2-b7-technical-contracts.md` §5

---

## 0. Execution Rules

- B2 must be independently verified first. During B3, v9 is tested on temporary databases and a disposable migrated Dev DB copy only.
- Do not stage, commit, push, download an unpinned model, call providers, or mutate either canonical DB.
- Reuse `packages/data-core/tests/db_fixtures.py`; implement `apply_migration_v9()` there before migration tests.
- Pin BGE-M3 tokenizer revision to the locally verified snapshot `5617a9f61b028005a4858fdac845db406aefb181`. A model name, branch, tag, abbreviated SHA, or placeholder is invalid.
- No test body may contain `pass`, comment-only assertions, `or` alternatives for an exact identity, or undefined fixture variables.
- Chunk tests must use the production tokenizer and compare explicit token ID sequences to prove maximum size and overlap.
- Corpus publication tests must inject a failure between reconciliation writes and manifest publication, then inspect transaction state.


## 1. Objective

Deliver the B3 corpus and chunking package that passes Core Exit Gate B (Corpus and chunking). Specifically:

- Canonical item-level normalization (N articles from one raw response → N canonical articles);
- `news_v2` chunk profile (384 max / 320 target / 48 overlap, pinned tokenizer);
- `filing_v2` chunk profile (section-boundary-aware, 8-K items + EX-99.x exhibits);
- Raw JSON, OHLCV, FRED arrays, FMP raw statements, SEC submissions never become chunks;
- Stable document_id, chunk_id, content_hash, metadata_hash;
- Source classification (7 classes per contract);
- Dedup cluster with separate representative and novelty timestamp;
- Corpus reconciliation with tombstones;
- Metadata-only updates without re-embedding;
- Interrupted build never publishes a current manifest;
- Deterministic CorpusManifest;
- Compatibility read for legacy rows (no bulk rewrite).

## 2. Current Verified State

### Implemented

| Component | File | Status |
|---|---|---|
| Articles schema + `article_tickers` | `catalyst_data/articles.py` | implemented |
| `articles` table | `catalyst_data/storage/sqlite.py` | implemented |
| OHLCV storage | `catalyst_data/storage/sqlite.py` | implemented |
| Index state | `catalyst_data/storage/sqlite.py` | implemented — `index_state` table |
| Dedup (hard + cross-source) | `catalyst_data/dedup/` | implemented |
| Eligibility module | `catalyst_data/eligibility.py` | implemented |
| Source tier | `catalyst_data/source_tier.py` | implemented |
| Index builder | `catalyst_data/index_builder.py` | existing — needs chunk profile rewrite |
| Embedding builder (GPU) | `packages/data-core/scripts/build_embeddings_gpu.py` | existing — consumes index_state |
| Finnhub normalize | `catalyst_data/pipeline/finnhub_normalize.py` | implemented |
| SEC normalize | `catalyst_data/pipeline/sec_normalize.py` | implemented |
| FRED normalize | `catalyst_data/pipeline/fred_normalize.py` | implemented |
| `raw_assets` legacy compatibility | `catalyst_data/storage/sqlite.py` | implemented |

### Partial

| Component | Gap |
|---|---|
| Article normalization | uses legacy per-ticker article IDs in places |
| Index builder | uses sentence-only splitter (`_split_sentences`), lacks token awareness, lacks section awareness |
| Chunk profiles | `news_v2` and `filing_v2` do not exist; current chunking is generic |
| Corpus manifest | `index_state` tracks individual chunks but has no atomic manifest publication |
| Source classification | `source_tier.py` exists but doesn't implement 7-class taxonomy |
| Dedup + novelty separation | dedup exists but `representative_document_id` and `cluster_first_available_at` may not be separated |
| Tombstones | not emitted for removed/ineligible/superseded-profile chunks |

### Missing

| Component | File to create |
|---|---|
| Corpus tables (migration v9) | `catalyst_data/migrations.py` (add v9) |
| `news_v2` chunk profile | `catalyst_data/corpus/news_v2.py` (new, target layout) |
| `filing_v2` chunk profile | `catalyst_data/corpus/filing_v2.py` (new) |
| ChunkProfile protocol | `catalyst_data/corpus/profile.py` (new) |
| Source classifier (7 classes) | `catalyst_data/corpus/source_classifier.py` (new) |
| Corpus reconciliation | `catalyst_data/corpus/reconciliation.py` (new) |
| CorpusManifest | `catalyst_data/corpus/manifest.py` (new) |
| Tombstone emission | `catalyst_data/corpus/tombstones.py` (new) |
| Metadata-only update logic | `catalyst_data/corpus/updater.py` (new) |
| Pinned tokenizer accessor | `catalyst_data/corpus/tokenizer.py` (new) |

### Obsolete

| Component | Reason |
|---|---|
| Sentence-only splitter in `index_builder.py` | replaced by `news_v2` / `filing_v2` |
| Whole-filing L1 embedding | replaced by section-aware `filing_v2` |
| Legacy per-ticker article chunk identities that duplicate canonical articles | must be reconciled |

## 3. Scope / Non-goals

### Scope

- Migration v9: corpus tables (chunks, corpus_manifest, tombstones, source_classification)
- ChunkProfile protocol with `news_v2` and `filing_v2` implementations
- Pinned tokenizer (BGE-M3 revision loaded once, tested for determinism)
- Source classifier: 7-class static algorithm
- Dedup: separate `cluster_first_available_at` and `representative_document_id`
- Corpus reconciliation: compare active chunks vs index_state, emit tombstones
- Metadata-only update path (ticker change → metadata_hash update, no re-embedding)
- Deterministic CorpusManifest with all contract fields
- Interrupted build guard (manifest only published after all writes succeed)
- Compatibility read for legacy raw_assets and article IDs
- One Polygon response with 30 articles → 30 canonical articles provenance check

### Non-goals

- FRED text chunks (macro release materializer is deferred)
- FMP fundamentals text chunks (earnings event materializer is deferred)
- Generic 10-K/10-Q parsing (Showcase, not Core)
- Embedding execution (B6 concern — B3 only produces chunk identities and text, not vectors)
- Full retrieval (B4 concern)
- Any frontend or API

## 4. Dependencies

### Inputs

- B2 completion: canonical articles with provenance, append-only raw store, request ledger
- Existing articles, article_tickers, filings, dedup logic
- Dev DB with migration v8 applied

### Output Artifacts

- Migration v9 proven on disposable databases; canonical Dev DB migration remains a separately authorized operator step
- Corpus manifest (JSON)
- Chunk identity inventory
- Tombstone inventory

### Consumed By

- B4 (retrieval): consumes CorpusManifest, chunk identities, cutoff filters
- B6 (dense/reranker): consumes chunks and content_hash for embedding

## 5. Schema and Artifact Ownership

### Owned SQLite Migration

**v9** (owned by B3):

- `corpus_chunks` — `(chunk_id TEXT PK, document_id, chunk_profile_version, section_key, ordinal, content_text, content_hash, metadata_hash, source_class, dedup_cluster_id, cluster_first_available_at, representative_document_id, available_at, ticker_associations, eligibility, manifest_id, status TEXT, created_at, updated_at)`
- `corpus_tombstones` — `(chunk_id TEXT PK, document_id, reason TEXT, tombstoned_at)`
- `corpus_manifest` — `(manifest_id TEXT PK, manifest_json TEXT, is_current INTEGER, created_at)`
- Extend `articles`: `source_class`, `dedup_cluster_id`, `cluster_first_available_at`, `representative_document_id`
- Extend `index_state`: `metadata_hash`, `is_tombstone`

### Owned Artifacts

- CorpusManifest (versioned JSON, one per build)
- Chunk identity inventory

### Not Owned

- Embedding vectors (B6)
- Retrieval index manifest (B6)
- IndexManifest (B6)
- FTS5 index (B4)
- BenchmarkCase schemas (B4 eval foundation)

## 6. File Allowlist

### Existing Files Allowed to Modify

```
packages/data-core/catalyst_data/migrations.py            — add v9
packages/data-core/catalyst_data/storage/sqlite.py        — extend tables
packages/data-core/catalyst_data/articles.py              — add source_class, dedup fields
packages/data-core/catalyst_data/eligibility.py           — integrate source classifier
packages/data-core/catalyst_data/dedup/cross_source.py    — separate representative/novelty
packages/data-core/catalyst_data/index_builder.py         — replace splitter with chunk profiles
packages/data-core/scripts/build_embeddings_gpu.py        — consume new chunk profiles
packages/data-core/scripts/build_index_from_artifacts.py  — adapter for new profiles
packages/data-core/catalyst_data/rederive.py              — compatibility read
packages/data-core/tests/test_migrations.py               — add v9 tests
packages/data-core/tests/test_index_builder.py            — rewrite for chunk profiles
packages/data-core/tests/test_dedup.py                    — add novelty/representative tests
```

### New Files Allowed to Create

```
packages/data-core/catalyst_data/corpus/__init__.py
packages/data-core/catalyst_data/corpus/profile.py
packages/data-core/catalyst_data/corpus/news_v2.py
packages/data-core/catalyst_data/corpus/filing_v2.py
packages/data-core/catalyst_data/corpus/source_classifier.py
packages/data-core/catalyst_data/corpus/reconciliation.py
packages/data-core/catalyst_data/corpus/manifest.py
packages/data-core/catalyst_data/corpus/tombstones.py
packages/data-core/catalyst_data/corpus/updater.py
packages/data-core/catalyst_data/corpus/tokenizer.py
packages/data-core/tests/test_news_v2.py
packages/data-core/tests/test_filing_v2.py
packages/data-core/tests/test_source_classifier.py
packages/data-core/tests/test_reconciliation.py
packages/data-core/tests/test_manifest.py
packages/data-core/tests/test_tokenizer.py
packages/data-core/tests/db_fixtures.py                    — add apply_migration_v9 helper
packages/data-core/tests/corpus_fixtures.py                — deterministic article, filing, reconciliation inventories
```

### Files Explicitly Forbidden

- `data/catalyst_eval_frozen_v2.db` — frozen, no migration, no repin
- `packages/agents/` — B5 domain
- `packages/eval/` — B4/B7 domain
- `packages/data-core/catalyst_data/connectors/` — B2 domain (read-only)
- `packages/data-core/catalyst_data/ingestion/` — B2 domain (read-only)
- Any legacy test that currently passes — update only for intentional contract drift; never weaken, skip, or xfail

## 7. TDD Tasks

### Task 0: Pin tokenizer identity and migration fixture

Before Task 1, define `TOKENIZER_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"`. Add `apply_migration_v9()` to the shared DB fixture and prove it upgrades a temporary v8 database without touching repository DB files. Assert that the tokenizer loader receives this revision explicitly.

`corpus_fixtures.py` must expose nine named reconciliation cases, each with an independent exact oracle: new chunk, unchanged chunk, content change, metadata-only change, document removal, eligibility loss, profile-version replacement, disappeared child, and dedup-cluster reassignment.

It must also expose deterministic splitter fixtures and literal oracles used by Tasks 3–4: `deterministic_boundary_article_fixture`, `long_title_article_fixture`, `no_boundary_article_fixture`, `raw_8k_duplicate_item_fixture`, `exhibit_99_1_fixture`, `EXPECTED_BOUNDARY_CHUNK_TEXTS`, `EXPECTED_BOUNDARY_KINDS`, `EXPECTED_BODY_STARTS`, `EXPECTED_BODY_ENDS`, `EXPECTED_BODY_OVERLAPS`, and `EXPECTED_EXHIBIT_BOUNDARY_KINDS`. The expected values are hand-authored from contract §§5.2–5.3 and may not call production chunking or boundary-selection code. Define the small test-only `stable_unique()` helper here as encounter-order deduplication. It must also expose `base_params`: a dict of every `build_manifest` keyword argument except `active_chunk_inventory` (`normalization_version`, `chunk_profile_versions`, `source_classifier_version`, `certified_snapshot_identity`, `tokenizer_revision=TOKENIZER_REVISION`, `embedding_revision=None`), used by the manifest tests so only the chunk inventory varies between cases.

### Task 1: Pinned tokenizer accessor

**Step 1: Write failing tokenizer test**

```python
# packages/data-core/tests/test_tokenizer.py

def test_tokenizer_is_pinned_revision():
    """Token count must match pinned BGE-M3 revision deterministically."""
    import re
    from catalyst_data.corpus.tokenizer import (
        get_tokenizer, count_tokens, TOKENIZER_MODEL_ID, TOKENIZER_REVISION,
    )

    assert TOKENIZER_MODEL_ID == "BAAI/bge-m3"
    assert re.fullmatch(r"[0-9a-f]{40}", TOKENIZER_REVISION)
    tok = get_tokenizer()
    assert tok is not None

    text = "Hello world, this is a test."
    count = count_tokens(text)
    assert count > 0
    # Determistic: same text → same count
    assert count == count_tokens(text)


def test_tokenizer_revision_in_manifest():
    """Tokenizer model ID and bare revision are persisted separately."""
    from catalyst_data.corpus.tokenizer import TOKENIZER_MODEL_ID, TOKENIZER_REVISION
    manifest = tokenizer_identity()
    assert manifest == {
        "model_id": TOKENIZER_MODEL_ID,
        "revision": TOKENIZER_REVISION,
    }
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_tokenizer.py -q
```

Expected: FAIL — module not created.

**Step 3: Implement `catalyst_data/corpus/tokenizer.py`**

- `TOKENIZER_MODEL_ID = "BAAI/bge-m3"`
- `TOKENIZER_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"`
- `get_tokenizer() → PreTrainedTokenizer`
- `count_tokens(text: str) → int`

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_tokenizer.py -q
```

Expected: all PASS.

### Task 2: Migration v9 — corpus tables

**Step 1: Write failing migration test**

```python
# packages/data-core/tests/test_migrations.py

def test_v9_adds_corpus_tables():
    """v9 creates corpus_chunks, corpus_tombstones, corpus_manifest."""
    db = _fresh_db_at_version(8)
    apply_migration_v9(db)

    for table in ["corpus_chunks", "corpus_tombstones", "corpus_manifest"]:
        assert table in _table_names(db)

    chunk_columns = _table_columns(db, "corpus_chunks")
    assert "manifest_id" in chunk_columns

def test_v9_extends_articles():
    """v9 adds source_class, dedup_cluster_id, cluster_first_available_at, representative_document_id to articles."""
    db = _fresh_db_at_version(8)
    apply_migration_v9(db)
    cols = _table_columns(db, "articles")
    for col in ["source_class", "dedup_cluster_id", "cluster_first_available_at",
                "representative_document_id"]:
        assert col in cols

def test_v9_extends_index_state():
    """v9 adds metadata_hash and is_tombstone to index_state."""
    db = _fresh_db_at_version(8)
    apply_migration_v9(db)
    cols = _table_columns(db, "index_state")
    assert "metadata_hash" in cols
    assert "is_tombstone" in cols
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_migrations.py -k "v9" -q
```

Expected: FAIL.

**Step 3: Implement v9 migration**

Add `MIGRATION_9_SQL` with full DDL. Register in migration registry.

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_migrations.py -k "v9" -q
```

Expected: all PASS.

### Task 3: ChunkProfile protocol and news_v2

**Step 1: Write failing news_v2 test**

```python
# packages/data-core/tests/test_news_v2.py

def test_short_article_one_chunk():
    """Article ≤ 384 tokens → exactly one searchable chunk."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    article = {"document_id": "poly:art1", "title": "Brief Update",
               "description": "Short description.", "available_at": "2026-01-01T09:00:00Z"}
    chunks = profile.chunk(article)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.chunk_id == "poly:art1:news_v2:body:0001"
    assert c.document_id == "poly:art1"


def test_short_article_chunk_text_is_title_newline_description():
    """Chunk text = title + '\n' + description."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    article = {"document_id": "poly:art1", "title": "X", "description": "Y",
               "available_at": "2026-01-01T09:00:00Z"}
    chunks = profile.chunk(article)
    assert chunks[0].content_text == "X\nY"


def test_long_article_split_with_overlap():
    """Article > 384 tokens → multiple chunks, each ≤ 384 tokens, overlap ≤ 48."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile
    from catalyst_data.corpus.tokenizer import count_tokens

    profile = NewsV2Profile()
    # Generate text that's ~800 tokens
    long_text = "Sentence one. " * 200
    article = {"document_id": "poly:art2", "title": "Long Report",
               "description": long_text, "available_at": "2026-01-01T09:00:00Z"}
    chunks = profile.chunk(article)
    assert len(chunks) >= 2

    for c in chunks:
        assert count_tokens(c.content_text) <= 384

    token_ids = [profile.body_token_ids(c) for c in chunks]
    for left, right in zip(token_ids, token_ids[1:]):
        overlap = longest_suffix_prefix_overlap(left, right)
        assert 0 <= overlap <= 48


def test_semantic_boundary_selection_is_exact_and_deterministic():
    """Paragraph wins over sentence; distance ties choose the earlier boundary."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    article = deterministic_boundary_article_fixture()
    first = NewsV2Profile().chunk(article)
    second = NewsV2Profile().chunk(article)

    assert [c.content_text for c in first] == EXPECTED_BOUNDARY_CHUNK_TEXTS
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    assert [c.boundary_kind for c in first] == EXPECTED_BOUNDARY_KINDS
    assert [c.body_token_start for c in first] == EXPECTED_BODY_STARTS
    assert [c.body_token_end for c in first] == EXPECTED_BODY_ENDS


def test_title_prefix_and_overlap_use_separate_budgets():
    """Repeated title counts toward 384; overlap is at most 48 body tokens only."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile
    from catalyst_data.corpus.tokenizer import count_tokens

    chunks = NewsV2Profile().chunk(long_title_article_fixture())
    assert all(c.prefix_token_count <= 64 for c in chunks)
    assert all(count_tokens(c.content_text) <= 384 for c in chunks)
    assert chunks[0].prefix_truncated is True
    assert [c.body_overlap_tokens for c in chunks] == EXPECTED_BODY_OVERLAPS


def test_no_semantic_boundary_uses_token_fallback_and_makes_progress():
    """A long punctuation-free body uses exact token fallback without looping."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    chunks = NewsV2Profile().chunk(no_boundary_article_fixture())
    assert all(c.boundary_kind in {"token_fallback", "document_end"} for c in chunks)
    assert all(right.body_token_start > left.body_token_start
               for left, right in zip(chunks, chunks[1:]))


def test_title_prepended_to_child_chunks():
    """Child chunks prepend title when absent, counting against 384."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    long_text = "Sentence. " * 150
    article = {"document_id": "poly:art3", "title": "IMPORTANT: Update",
               "description": long_text, "available_at": "2026-01-01T09:00:00Z"}
    chunks = profile.chunk(article)
    # Child chunks should include title
    for c in chunks[1:]:
        assert "IMPORTANT: Update" in c.content_text


def test_no_whole_parent_with_children():
    """Single-chunk: parent IS the chunk. Multi-chunk: no separate whole-parent chunk."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    # Short → exactly 1 chunk (it IS the representation)
    article_short = {"document_id": "poly:s", "title": "S", "description": "D",
                     "available_at": "2026-01-01T09:00:00Z"}
    assert len(profile.chunk(article_short)) == 1

    # Long → multiple children, no additional whole-parent
    article_long = {"document_id": "poly:l", "title": "L", "description": "Sentence. " * 200,
                    "available_at": "2026-01-01T09:00:00Z"}
    children = profile.chunk(article_long)
    assert len(children) >= 2
    parent_chunks = [c for c in children if c.chunk_id.endswith("_parent")]
    assert len(parent_chunks) == 0


def test_metadata_fields_not_in_chunk_text():
    """Image URL, article URL, publisher, logo, ticker JSON stay in metadata."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    article = {"document_id": "poly:art4", "title": "Test", "description": "Body",
               "available_at": "2026-01-01T09:00:00Z",
               "image_url": "https://img.example.com/photo.jpg",
               "article_url": "https://example.com/article/1",
               "publisher": "CNBC",
               "ticker_associations": '["AAPL"]'}
    chunks = profile.chunk(article)
    for c in chunks:
        assert "photo.jpg" not in c.content_text
        assert "example.com/article" not in c.content_text


def test_chunk_id_includes_profile_version():
    """chunk_id = document_id + ':' + chunk_profile_version + ':' + section_key + ':' + ordinal."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile(profile_version="news_v2")
    article = {"document_id": "poly:art5", "title": "T", "description": "D",
               "available_at": "2026-01-01T09:00:00Z"}
    chunks = profile.chunk(article)
    cid = chunks[0].chunk_id
    assert "news_v2" in cid
    assert cid.startswith("poly:art5")
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_news_v2.py -q
```

Expected: FAIL.

**Step 3: Implement**

- `catalyst_data/corpus/profile.py`: `ChunkProfile` protocol
- `catalyst_data/corpus/news_v2.py`: implement contract §5.2 exactly, including NFC/whitespace normalization, pinned-tokenizer offset mapping, paragraph-before-sentence boundary selection, 64-token prefix cap, four-digit ordinals, body-only overlap, fallback progress, and audit metadata
- `catalyst_data/corpus/tokenizer.py`: expose token counts and character/token offset mapping from the pinned tokenizer; do not decode token IDs to reconstruct chunk text
- Task 0 fixtures must define all `EXPECTED_*` values as literal independent oracles; they may not call `NewsV2Profile` or the production boundary selector

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_news_v2.py -q
```

Expected: all PASS.

### Task 4: filing_v2 chunk profile

**Step 1: Write failing filing_v2 test**

```python
# packages/data-core/tests/test_filing_v2.py

def test_filing_chunks_never_cross_sections():
    """Chunks preserve 8-K item boundaries and EX-99 section boundaries."""
    from catalyst_data.corpus.filing_v2 import FilingV2Profile

    profile = FilingV2Profile()
    filing = {
        "document_id": "sec:0000320193-25-000001:8k",
        "sections": [
            {"section_key": "item_1.01", "title": "Item 1.01", "text": "Entry into Material Definitive Agreement. " * 80},
            {"section_key": "item_2.03", "title": "Item 2.03", "text": "Creation of Direct Financial Obligation. " * 60},
        ],
        "available_at": "2026-01-01T09:00:00Z"
    chunks = profile.chunk(filing)
    # Each chunk's section_key matches its source section
    for c in chunks:
        assert c.section_key in ("item_1.01", "item_2.03")


def test_filing_chunks_respect_384_max():
    """No chunk exceeds 384 tokens."""
    from catalyst_data.corpus.filing_v2 import FilingV2Profile
    from catalyst_data.corpus.tokenizer import count_tokens

    profile = FilingV2Profile()
    filing = {
        "document_id": "sec:big",
        "sections": [{"section_key": "item_1.01", "text": "Very long text. " * 500}],
        "available_at": "2026-01-01T09:00:00Z"
    }
    for c in profile.chunk(filing):
        assert count_tokens(c.content_text) <= 384


def test_filing_no_whole_document_chunk():
    """Never embed the entire filing as one fallback vector."""
    from catalyst_data.corpus.filing_v2 import FilingV2Profile

    profile = FilingV2Profile()
    filing = {
        "document_id": "sec:test",
        "sections": [{"section_key": "item_1.01", "text": "Text here."}],
        "available_at": "2026-01-01T09:00:00Z"
    }
    chunks = profile.chunk(filing)
    # No chunk whose content is the whole filing
    for c in chunks:
        assert c.section_key != "whole_filing"


def test_parse_failure_produces_degraded_chunks():
    """Parsing failure emits unknown-section chunks with degraded flag, not whole filing."""
    from catalyst_data.corpus.filing_v2 import FilingV2Profile

    profile = FilingV2Profile()
    filing = {
        "document_id": "sec:broken",
        "sections": [],  # parse failure — no sections extracted
        "raw_text": "Some unstructured filing text. " * 50,
        "available_at": "2026-01-01T09:00:00Z"
    }
    chunks = profile.chunk(filing)
    # Emits chunks with section_key=unknown, not zero chunks
    assert len(chunks) >= 1
    for c in chunks:
        assert c.section_key == "unknown_000"
        assert c.is_degraded


def test_raw_8k_item_detection_and_duplicate_suffixes_are_exact():
    """Raw 8-K headings use the contract regex and duplicate keys get stable suffixes."""
    from catalyst_data.corpus.filing_v2 import FilingV2Profile

    chunks = FilingV2Profile().chunk(raw_8k_duplicate_item_fixture())
    assert stable_unique([c.section_key for c in chunks]) == [
        "unknown_000", "item_1.01", "item_1.01_02", "item_2.03"
    ]
    assert all(c.chunk_id.split(":")[-1].isdigit() and len(c.chunk_id.split(":")[-1]) == 4
               for c in chunks)


def test_exhibit_document_type_defines_one_section():
    """EX-99.1 uses ex_99_1 and windows within that section only."""
    from catalyst_data.corpus.filing_v2 import FilingV2Profile

    chunks = FilingV2Profile().chunk(exhibit_99_1_fixture())
    assert {c.section_key for c in chunks} == {"ex_99_1"}
    assert [c.boundary_kind for c in chunks] == EXPECTED_EXHIBIT_BOUNDARY_KINDS
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_filing_v2.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_data/corpus/filing_v2.py`**

- `FilingV2Profile`: implement contract §5.3 exactly, including the line-anchored 8-K Item regex, ordered duplicate suffixes, EX-99.x document-type keys, preamble handling, and the shared deterministic 384/320/48 window algorithm
- Section heading prefix is capped at 64 tokens and prepended to each windowed chunk
- Parse failure → bounded `section_key=unknown_000` chunks with `section_parse_degraded=true`; never one whole-filing vector
- Task 0 supplies literal expected section keys, boundary kinds, and ordinals for all fixtures

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_filing_v2.py -q
```

Expected: all PASS.

### Task 5: Source classifier

**Step 1: Write failing classifier test**

```python
# packages/data-core/tests/test_source_classifier.py

def test_classify_ohlcv():
    """OHLCV source → structured_market_data."""
    from catalyst_data.corpus.source_classifier import classify
    assert classify(source_kind="ohlcv") == "structured_market_data"


def test_classify_sec():
    """SEC filing → official_government."""
    from catalyst_data.corpus.source_classifier import classify
    assert classify(source_kind="sec") == "official_government"


def test_classify_globe_newswire():
    """GlobeNewswire URL → corporate_press_release."""
    from catalyst_data.corpus.source_classifier import classify
    result = classify(source_kind="news", article_url="https://www.globenewswire.com/news-release/...")
    assert result == "corporate_press_release"


def test_classify_cnbc():
    """CNBC URL → reported_news."""
    from catalyst_data.corpus.source_classifier import classify
    result = classify(source_kind="news", article_url="https://www.cnbc.com/2026/01/01/...")
    assert result == "reported_news"


def test_classify_seeking_alpha():
    """Seeking Alpha → analysis_opinion."""
    from catalyst_data.corpus.source_classifier import classify
    result = classify(source_kind="news", article_url="https://seekingalpha.com/news/...")
    assert result == "analysis_opinion"


def test_classify_yahoo_unknown():
    """Yahoo/unrecognized → aggregated_unknown."""
    from catalyst_data.corpus.source_classifier import classify
    result = classify(source_kind="news", article_url="https://finance.yahoo.com/news/...",
                      publisher="UnknownOrg")
    assert result == "aggregated_unknown"


def test_classifier_publisher_fallback():
    """When URL doesn't match, publisher metadata provides classification."""
    from catalyst_data.corpus.source_classifier import classify
    result = classify(source_kind="news", article_url="https://unknown.example.com/article",
                      publisher="MarketWatch")
    assert result == "reported_news"


def test_classifier_version_pinned():
    """source_classifier_version is pinned, not 'latest'."""
    from catalyst_data.corpus.source_classifier import CLASSIFIER_VERSION
    assert "latest" not in CLASSIFIER_VERSION.lower()
    assert CLASSIFIER_VERSION.count(".") >= 1
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_source_classifier.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_data/corpus/source_classifier.py`**

- `CLASSIFIER_VERSION` pinned string
- `classify(source_kind, article_url=None, publisher=None) → str` — static algorithm per contract
- 7 classes: `structured_market_data`, `official_government`, `issuer_disclosure`, `corporate_press_release`, `reported_news`, `analysis_opinion`, `aggregated_unknown`

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_source_classifier.py -q
```

Expected: all PASS.

### Task 6: Stable identities and content/metadata hash

**Step 1: Write failing identity test**

```python
# packages/data-core/tests/test_reconciliation.py

def test_document_id_is_provider_native():
    """document_id uses provider-native canonical identity."""
    # Polygon article → "poly:{article_id}"
    # SEC filing → "sec:{accession_number}:{document_type}"
    # Already implemented, verify no regression
    from catalyst_data.articles import compute_article_id
    aid = compute_article_id("polygon", "abc123")
    assert aid == "polygon:abc123"


def test_content_hash_is_embedding_text_sha256():
    """content_hash = SHA256 of exact normalized embedding text."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile
    import hashlib

    profile = NewsV2Profile()
    article = {"document_id": "poly:art1", "title": "T", "description": "D",
               "available_at": "2026-01-01T09:00:00Z"}
    chunks = profile.chunk(article)
    expected_hash = hashlib.sha256("T\nD".encode("utf-8")).hexdigest()
    assert chunks[0].content_hash == expected_hash


def test_image_change_does_not_alter_content_hash():
    """Changing image_url preserves content_hash."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    article1 = {"document_id": "poly:img1", "title": "T", "description": "D",
                "available_at": "2026-01-01T09:00:00Z", "image_url": "a.jpg"}
    article2 = {"document_id": "poly:img1", "title": "T", "description": "D",
                "available_at": "2026-01-01T09:00:00Z", "image_url": "b.jpg"}
    c1 = profile.chunk(article1)[0]
    c2 = profile.chunk(article2)[0]
    assert c1.content_hash == c2.content_hash


def test_ticker_change_alters_metadata_hash():
    """Ticker association change alters metadata_hash but not content_hash."""
    from catalyst_data.corpus.news_v2 import NewsV2Profile

    profile = NewsV2Profile()
    article1 = {"document_id": "poly:t1", "title": "T", "description": "D",
                "available_at": "2026-01-01T09:00:00Z", "ticker_associations": '["AAPL"]'}
    article2 = {"document_id": "poly:t1", "title": "T", "description": "D",
                "available_at": "2026-01-01T09:00:00Z", "ticker_associations": '["AAPL","MSFT"]'}
    c1 = profile.chunk(article1)[0]
    c2 = profile.chunk(article2)[0]
    assert c1.content_hash == c2.content_hash
    assert c1.metadata_hash != c2.metadata_hash
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_reconciliation.py -k "identity" -q
```

Expected: FAIL — chunk profiles not yet producing content_hash/metadata_hash.

**Step 3: Implement identity computation in chunk profiles**

Add `content_hash` and `metadata_hash` to `ChunkProfile` output per contract §5.4.

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_reconciliation.py -k "identity" -q
```

Expected: all PASS.

### Task 7: Dedup — separate representative and novelty

**Step 1: Write failing dedup test**

```python
# packages/data-core/tests/test_dedup.py

def test_representative_change_does_not_alter_novelty():
    """Changing representative_document_id does not change cluster_first_available_at."""
    from catalyst_data.dedup.cross_source import compute_cluster_fields

    cluster = [
        {"document_id": "poly:a", "available_at": "2026-01-01T08:00:00Z", "source_class": "aggregated_unknown"},
        {"document_id": "poly:b", "available_at": "2026-01-01T09:00:00Z", "source_class": "reported_news"},
    ]
    result1 = compute_cluster_fields(cluster)
    first_available = result1["cluster_first_available_at"]

    # Change representative selection (prefer reported_news)
    result2 = compute_cluster_fields(cluster)
    assert result2["cluster_first_available_at"] == first_available
    # Representative may differ but not novelty
    assert result2["representative_document_id"] is not None


def test_single_cluster_one_evidence_item():
    """One dedup cluster → at most one default evidence-pack item."""
    items = build_default_evidence_pack([
        make_chunk("a", cluster_id="cluster-1", source_class="reported_news"),
        make_chunk("b", cluster_id="cluster-1", source_class="aggregated_unknown"),
    ])
    assert [item.dedup_cluster_id for item in items] == ["cluster-1"]
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_dedup.py -k "representative" -q
```

Expected: FAIL if current implementation conflates the two fields.

**Step 3: Implement separate fields**

Modify `catalyst_data/dedup/cross_source.py` to compute and store both `cluster_first_available_at` and `representative_document_id` independently.

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_dedup.py -q
```

Expected: all PASS.

### Task 8: Corpus reconciliation and tombstones

**Step 1: Write failing reconciliation test**

```python
# packages/data-core/tests/test_reconciliation.py

def test_reconciliation_detects_removed_chunks():
    """Chunks previously active but now absent → tombstones emitted."""
    from catalyst_data.corpus.reconciliation import reconcile

    db = _fresh_db_at_version(9)
    # Seed index_state with a chunk that no longer exists
    db.execute("INSERT INTO index_state (chunk_id, document_id, status) VALUES (?, ?, 'active')",
               ("poly:old:v1::001", "poly:old"))

    active_chunks = {}  # empty — old chunk removed
    tombstones = reconcile(db, active_chunks, manifest_id="manifest-v1")
    assert any(t.chunk_id == "poly:old:v1::001" for t in tombstones)


def test_reconciliation_detects_content_changes():
    """Content change → chunk marked for re-embedding."""
    from catalyst_data.corpus.reconciliation import reconcile

    db = _fresh_db_at_version(9)
    db.execute("INSERT INTO index_state (chunk_id, document_id, content_hash, status) VALUES (?, ?, ?, 'embedded')",
               ("poly:ch1:v1::001", "poly:ch1", "oldhash"))

    active_chunks = {"poly:ch1:v1::001": {"content_hash": "newhash", "metadata_hash": "same"}}
    to_reembed = reconcile(db, active_chunks, manifest_id="manifest-v1")
    assert "poly:ch1:v1::001" in to_reembed.to_embed


def test_reconciliation_metadata_only_no_reembed():
    """Metadata-only change → update metadata, skip re-embedding."""
    from catalyst_data.corpus.reconciliation import reconcile

    db = _fresh_db_at_version(9)
    db.execute("INSERT INTO index_state (chunk_id, document_id, content_hash, metadata_hash, status) VALUES (?, ?, ?, ?, 'embedded')",
               ("poly:m1:v1::001", "poly:m1", "samehash", "oldmeta"))

    active_chunks = {"poly:m1:v1::001": {"content_hash": "samehash", "metadata_hash": "newmeta"}}
    result = reconcile(db, active_chunks, manifest_id="manifest-v1")
    assert "poly:m1:v1::001" not in result.to_embed  # no re-embedding
    assert "poly:m1:v1::001" in result.to_update_metadata


def test_profile_version_change_tombstones_old():
    """Profile version change → old chunks tombstoned, new chunks with new IDs."""
    from catalyst_data.corpus.reconciliation import reconcile

    db = _fresh_db_at_version(9)
    db.execute("INSERT INTO index_state (chunk_id, document_id, status) VALUES (?, ?, 'embedded')",
               ("poly:art:v1::001", "poly:art"))

    active_chunks = {"poly:art:v2::001": {"content_hash": "newhash", "metadata_hash": "newmeta"}}
    tombstones = reconcile(db, active_chunks, manifest_id="manifest-v2")
    assert any(t.chunk_id == "poly:art:v1::001" for t in tombstones)
    assert "poly:art:v2::001" not in {t.chunk_id for t in tombstones}


@pytest.mark.parametrize("case_name", NINE_RECONCILIATION_CASE_NAMES)
def test_reconcile_handles_each_failure_mode(case_name):
    """Each reconciliation mode has an independent fixture and exact oracle."""
    case = reconciliation_case(case_name)
    result = reconcile(
        case.db,
        active_chunks=case.active_chunks,
        manifest_id=case.next_manifest_id,
    )
    assert set(result.to_embed) == set(case.expected_to_embed), case_name
    assert set(result.to_update_metadata) == set(case.expected_metadata_updates), case_name
    assert {item.chunk_id for item in result.tombstones} == set(case.expected_tombstones), case_name
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_reconciliation.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_data/corpus/reconciliation.py`**

- `reconcile(db, active_chunks, manifest_id) → ReconciliationResult`
- Compare active_chunks dict against index_state
- Emit tombstones for removed/ineligible/superseded-profile chunks
- Return lists: `to_embed`, `to_update_metadata`, `tombstones`

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_reconciliation.py -q
```

Expected: all PASS.

### Task 9: CorpusManifest

**Step 1: Write failing manifest test**

```python
# packages/data-core/tests/test_manifest.py

def test_manifest_id_matches_contract():
    """manifest_id = SHA256 of canonical JSON per contract §5.5."""
    from catalyst_data.corpus.manifest import build_manifest, compute_manifest_id
    from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION

    manifest = build_manifest(
        normalization_version="1.0.0",
        chunk_profile_versions={"news": "news_v2", "filing": "filing_v2"},
        source_classifier_version="1.0.0",
        certified_snapshot_identity="snap-abc",
        active_chunk_inventory=["poly:a:v1::001", "sec:b:v1::001"],
        tokenizer_revision=TOKENIZER_REVISION,
        embedding_revision=None,
    )
    mid = compute_manifest_id(manifest)
    assert len(mid) == 64  # SHA256

    # created_at excluded from hash
    manifest2 = build_manifest(
        normalization_version="1.0.0",
        chunk_profile_versions={"news": "news_v2", "filing": "filing_v2"},
        source_classifier_version="1.0.0",
        certified_snapshot_identity="snap-abc",
        active_chunk_inventory=["poly:a:v1::001", "sec:b:v1::001"],
        tokenizer_revision=TOKENIZER_REVISION,
        embedding_revision=None,
    )
    assert compute_manifest_id(manifest2) == mid


def test_manifest_different_chunks_different_id():
    """Different chunk inventory → different manifest_id."""
    from catalyst_data.corpus.manifest import build_manifest, compute_manifest_id

    m1 = build_manifest(active_chunk_inventory=["a", "b"], **base_params)
    m2 = build_manifest(active_chunk_inventory=["a", "c"], **base_params)
    assert compute_manifest_id(m1) != compute_manifest_id(m2)


def test_manifest_published_atomically():
    """Interrupted reconciliation preserves the previous current manifest."""
    from catalyst_data.corpus.manifest import publish_manifest, reconcile_and_publish

    db = _fresh_db_at_version(9)
    publish_manifest(db, manifest_id="m1", manifest_json='{"test":true}')
    with pytest.raises(InjectedReconciliationFailure):
        reconcile_and_publish(
            db,
            next_manifest_id="m2",
            active_chunks=changed_inventory(),
            fail_after_chunk_writes=True,
        )
    current = db.execute(
        "SELECT manifest_id FROM corpus_manifest WHERE is_current = 1").fetchone()
    assert current["manifest_id"] == "m1"
    assert db.execute(
        "SELECT COUNT(*) FROM corpus_manifest WHERE manifest_id = 'm2'"
    ).fetchone()[0] == 0
```

**Step 2: Run test**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_manifest.py -q
```

Expected: FAIL.

**Step 3: Implement `catalyst_data/corpus/manifest.py`**

- `build_manifest(...) → dict`
- `compute_manifest_id(manifest) → str` — per contract §5.5
- `publish_manifest(db, manifest_id, manifest_json) → None` — sets `is_current=1`, clears others

**Step 4: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_manifest.py -q
```

Expected: all PASS.

### Task 10: Integration — full corpus build pipeline

**Step 1: Write integration test**

```python
# packages/data-core/tests/test_reconciliation.py

def test_raw_json_not_chunked():
    """Raw JSON, OHLCV, FRED arrays, FMP raw statements, SEC submissions → zero chunks."""
    from catalyst_data.corpus.profile import should_chunk
    assert not should_chunk(source_type="ohlcv")
    assert not should_chunk(source_type="fred")
    assert not should_chunk(source_type="fmp_raw")
    assert not should_chunk(source_type="sec_submissions")


def test_one_polygon_response_n_articles():
    """Contract: one raw response with N articles → N canonical articles."""
    db = seeded_polygon_response_db(item_count=30)
    build_corpus(db)
    assert scalar(db, "SELECT COUNT(*) FROM raw_assets WHERE request_id = 'request-30'") == 1
    assert article_ids_for_raw(db, "raw-30") == expected_polygon_article_ids(30)
    assert scalar(db, "SELECT COUNT(*) FROM normalized_provenance WHERE raw_asset_id = 'raw-30'") == 30


def test_compatibility_read_old_raw_ids():
    """Legacy raw_asset_id values are readable; new writes use v2 format."""
    db = seeded_legacy_and_v2_raw_db()
    assert resolve_raw_payload(db, "legacy-cell-id") == b"legacy"
    assert resolve_raw_payload(db, "request-scoped-id") == b"v2"
```

**Step 2: Run tests**

```bash
.venv/bin/python -m pytest packages/data-core/tests/test_reconciliation.py -k "integration" -q
```

Expected: all PASS.

**Step 3: Wire into `index_builder.py`**

Replace generic sentence splitter with `ChunkProfile` protocol dispatch. Wire source classifier, reconciliation, manifest publication.

**Step 4: Run full test suite**

```bash
.venv/bin/python -m pytest packages/data-core -q
```

## 8. Landmine Tests

1. **OHLCV arrays enter chunks** — `rg "ohlcv.*chunk\|chunk.*ohlcv" packages/data-core/catalyst_data/corpus/` must return zero; verify no OHLCV-derived chunks in corpus.
2. **Whole-filing embedding** — `rg "whole_filing\|L1.*filing\|entire filing" packages/data-core/catalyst_data/corpus/` must return zero.
3. **Representative changes novelty** — `test_representative_change_does_not_alter_novelty` in test_dedup.py.
4. **Interrupted build publishes manifest** — verify `publish_manifest` only called at end of successful reconciliation.
5. **Tokenizer not pinned** — `rg "latest\|auto\|from_pretrained.*\"" packages/data-core/catalyst_data/corpus/tokenizer.py` must show explicit revision.
6. **Chunk contains two article IDs** — each chunk must belong to exactly one document_id.
7. **FRED observation arrays chunked** — verify macro profile emits zero chunks.
8. **FMP raw statements chunked** — verify fundamentals profile emits zero chunks.

## 9. Verification Ladder

```bash
# 1. New focused tests
.venv/bin/python -m pytest packages/data-core/tests/test_tokenizer.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_migrations.py -k "v9" -q
.venv/bin/python -m pytest packages/data-core/tests/test_news_v2.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_filing_v2.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_source_classifier.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_reconciliation.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_manifest.py -q

# 2. Existing related modules
.venv/bin/python -m pytest packages/data-core/tests/test_index_builder.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_dedup.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_articles_schema.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_finnhub_normalize.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_sec_normalize.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_fred_normalize.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_source_tier.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_transmuter.py -q
.venv/bin/python -m pytest packages/data-core/tests/test_rederive.py -q

# 3. Package canonical suite
.venv/bin/python -m pytest packages/data-core -q

# 4. Dependency regression
.venv/bin/python -m pytest packages/agents -q
.venv/bin/python -m pytest packages/eval -q
.venv/bin/python -m pytest packages/app -q

# 5. DB SHA
shasum -a 256 data/catalyst_dev_ws4b.db
shasum -a 256 data/catalyst_eval_frozen_v2.db

# 6. Git
git diff --check
```

## 10. Evidence Report Template

```markdown
## B3 Completion Report

### Files Changed
- [list]

### Migrations
- v9 applied

### Focused Test Counts
- test_tokenizer: X passed
- test_migrations (v9): X passed
- test_news_v2: X passed
- test_filing_v2: X passed
- test_source_classifier: X passed
- test_reconciliation: X passed
- test_manifest: X passed

### Canonical Package Counts
- data-core: X passed (was 758)
- agents: X passed (was 237)
- eval: X passed (was 93)
- app: X passed (was 128)

### Landmine Results
- [list]

### Before/After DB SHA
- Dev DB before: [B2 SHA]
- Dev DB after: 92731fb7c5c3e989b4fdcbefb9d1d2060974082a6c60f1bbecad6bc42ee846b0 (unchanged during implementation)
- Frozen DB: 0d97a7ec61… (unchanged)

### Artifacts Generated
- CorpusManifest (manifest_id: ...)
- Chunk inventory (N chunks across M documents)

### Unresolved Risks
- [list]

### Confirmation
- [ ] Next package (B4) not started
```

## 11. Git Boundaries

1. `feat(data-core): add migration v9 — corpus tables`
2. `feat(data-core): add pinned tokenizer accessor`
3. `feat(data-core): add ChunkProfile protocol and news_v2 profile`
4. `feat(data-core): add filing_v2 chunk profile`
5. `feat(data-core): add source classifier (7 classes)`
6. `feat(data-core): separate dedup representative and novelty timestamp`
7. `feat(data-core): add corpus reconciliation with tombstones`
8. `feat(data-core): add deterministic CorpusManifest`
9. `feat(data-core): wire chunk profiles into index_builder`
