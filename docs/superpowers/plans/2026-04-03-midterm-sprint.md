# Catalyst Midterm Sprint — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a working Catalyst system for midterm demo (Apr 10): data pipeline + eval harness + agent workflow with experiment comparison report.

**Architecture:** Three-phase build — (1) consolidate and rebuild data-core with new schema, connectors, pipeline stages, (2) build eval framework with golden set and 5 metrics, (3) build LangGraph Miner-Critic-Judge agent and run experiments. Each phase produces testable, committable software.

**Tech Stack:** Python 3.13, httpx, pydantic, SQLite (WAL), LanceDB, bge-m3/bge-reranker-v2, LangGraph, RAGAS, pytest

**Spec:** `docs/superpowers/specs/2026-04-02-catalyst-system-design.md` (v1.1)

---

## Phase 0: Repo Cleanup & Consolidation (Day 0)

### Task 0.1: Delete dead files and track untracked docs

**Files:**
- Delete (tracked): `packages/data-core/catalyst_data/` (entire folder — 17 empty files)
- Delete (tracked): `packages/data-core/SCHEMA_DESIGN.md`
- Delete (tracked): `README.md` (root — already deleted on disk)
- Delete (untracked): `packages/data-core/core_references/` (entire folder — lives in ~/Desktop/references/)
- Delete (untracked): `CLAUDE.md` (empty)
- Add (untracked): `docs/API_Documentation/*.md` (7 files)
- Add (untracked): `docs/catalyst_whitepaper.md`
- Add (untracked): `docs/philosophy_of_teacher_mac.md`
- Add (untracked): `docs/reference_projects.md`

- [ ] **Step 1: Remove tracked dead files**

```bash
git rm -r packages/data-core/catalyst_data/
git rm packages/data-core/SCHEMA_DESIGN.md
git rm README.md
```

- [ ] **Step 2: Delete untracked junk from disk**

```bash
rm -rf packages/data-core/core_references/
rm -f CLAUDE.md
```

- [ ] **Step 3: Add untracked docs to git**

```bash
git add docs/API_Documentation/ docs/catalyst_whitepaper.md docs/philosophy_of_teacher_mac.md docs/reference_projects.md
```

- [ ] **Step 4: Commit cleanup**

```bash
git commit -m "chore: remove dead scaffolding and track project docs"
```

### Task 0.2: Rename data_core → catalyst_data and restructure

The active code lives in `packages/data-core/data_core/`. The final package name per the spec is `catalyst_data`. We rename and restructure into the spec's directory layout.

**Files:**
- Rename: `packages/data-core/data_core/` → `packages/data-core/catalyst_data/`
- Create subdirs: `pipeline/`, `storage/`, `dedup/`
- Move existing files into new structure

- [ ] **Step 1: Rename the package directory**

```bash
cd /Users/yiannischen/Desktop/Catalyst
git mv packages/data-core/data_core packages/data-core/catalyst_data
```

- [ ] **Step 2: Create new subdirectories**

```bash
mkdir -p packages/data-core/catalyst_data/pipeline
mkdir -p packages/data-core/catalyst_data/storage
mkdir -p packages/data-core/catalyst_data/dedup
touch packages/data-core/catalyst_data/pipeline/__init__.py
touch packages/data-core/catalyst_data/storage/__init__.py
touch packages/data-core/catalyst_data/dedup/__init__.py
```

- [ ] **Step 3: Move existing files into new structure**

```bash
# Connectors stay where they are (already in connectors/)
# Move connector_types.py into connectors/ as base.py
git mv packages/data-core/catalyst_data/connector_types.py packages/data-core/catalyst_data/connectors/base.py

# Move sqlite_cache.py into storage/
git mv packages/data-core/catalyst_data/sqlite_cache.py packages/data-core/catalyst_data/storage/sqlite.py

# transmuter.py stays at package root (shared utility)
# models.py stays at package root
# retry.py stays at package root
# source_mapping.py — add to git first, stays at package root
git add packages/data-core/catalyst_data/source_mapping.py
```

- [ ] **Step 4: Update all imports in existing files**

Every `.py` file that imports from `data_core` must be updated to `catalyst_data`. Also update internal imports (e.g., `from data_core.connector_types` → `from catalyst_data.connectors.base`).

Files to update:
- `packages/data-core/catalyst_data/connectors/fmp.py`: `from data_core.connector_types import FetchResult` → `from catalyst_data.connectors.base import FetchResult`
- `packages/data-core/catalyst_data/connectors/yfinance_fallback.py`: same pattern
- `packages/data-core/catalyst_data/models.py`: no external imports to change
- `packages/data-core/catalyst_data/retry.py`: no external imports
- `packages/data-core/catalyst_data/transmuter.py`: no external imports
- `packages/data-core/catalyst_data/storage/sqlite.py`: `from data_core.models import DataAsset` → `from catalyst_data.models import DataAsset`
- All test files in `packages/data-core/tests/`: update `from data_core.` → `from catalyst_data.`

- [ ] **Step 5: Delete files that won't be used in v2**

These files from the old messy development are superseded by the new pipeline/ structure we'll build in Phase 1:
- `packages/data-core/catalyst_data/orchestrator.py` (if it exists after rename)
- `packages/data-core/catalyst_data/pipeline.py` (if it exists — old monolithic pipeline)
- `packages/data-core/catalyst_data/artifacts.py` (if it exists)
- `packages/data-core/catalyst_data/logger.py` (if it exists)

Check which exist and only delete those that do:
```bash
ls packages/data-core/catalyst_data/*.py
# Delete any of: orchestrator.py, pipeline.py, artifacts.py, logger.py, types.py
```

Also remove tests that reference deleted files.

- [ ] **Step 6: Run existing tests to verify nothing broke**

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/data-core
python -m pytest tests/ -v --tb=short 2>&1 | head -40
```

Some tests may fail due to import changes or deleted files — that's expected. Fix any trivial import issues. Tests for deleted modules should be removed.

- [ ] **Step 7: Commit restructure**

```bash
git add -A packages/data-core/
git commit -m "refactor(data-core): consolidate to catalyst_data package with new directory structure"
```

### Task 0.3: Create pyproject.toml for data-core package

**Files:**
- Create: `packages/data-core/pyproject.toml`

- [ ] **Step 1: Write pyproject.toml**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "catalyst-data"
version = "0.1.0"
description = "Financial data ingestion, cleaning, and Markdown conversion for AI agents"
requires-python = ">=3.11"
license = "MIT"
authors = [{ name = "Yiannis Chen" }]

dependencies = [
    "httpx>=0.27",
    "pydantic>=2.0",
    "beautifulsoup4>=4.12",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]
vector = [
    "lancedb>=0.6",
    "FlagEmbedding>=1.2",
]

[tool.hatch.build.targets.wheel]
packages = ["catalyst_data"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Step 2: Commit**

```bash
git add packages/data-core/pyproject.toml
git commit -m "chore(data-core): add pyproject.toml for catalyst-data package"
```

---

## Phase 1: Data-Core v2 (Days 1-3)

### Task 1.1: New database schema (Bronze/Silver split)

**Files:**
- Modify: `packages/data-core/catalyst_data/storage/sqlite.py`
- Test: `packages/data-core/tests/test_storage.py`

- [ ] **Step 1: Write failing tests for new schema**

```python
# packages/data-core/tests/test_storage.py
import sqlite3
from catalyst_data.storage.sqlite import init_db, upsert_raw_asset, upsert_clean_asset, get_raw_asset, get_clean_asset, upsert_ohlcv, get_ohlcv

def test_init_db_creates_all_tables():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "raw_assets" in tables
    assert "clean_assets" in tables
    assert "ohlcv" in tables
    assert "news_alignment" in tables
    assert "attributions" in tables
    assert "golden_events" in tables
    conn.close()

def test_init_db_sets_wal_mode():
    import tempfile, os
    path = os.path.join(tempfile.mkdtemp(), "test.db")
    conn = sqlite3.connect(path)
    init_db(conn)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    conn.close()

def test_upsert_and_get_raw_asset():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    upsert_raw_asset(conn, asset_id="abc123", ticker="AAPL", source_type="polygon_news",
                     reference_date="2026-01-15", data_version="v1",
                     content_raw=b'{"test": true}', http_status=200, metadata={})
    asset = get_raw_asset(conn, "abc123")
    assert asset is not None
    assert asset["ticker"] == "AAPL"
    assert asset["source_type"] == "polygon_news"
    conn.close()

def test_upsert_and_get_clean_asset():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    upsert_raw_asset(conn, asset_id="abc123", ticker="AAPL", source_type="polygon_news",
                     reference_date="2026-01-15", data_version="v1",
                     content_raw=b'{"test": true}', http_status=200, metadata={})
    upsert_clean_asset(conn, asset_id="abc123", ticker="AAPL", source_type="polygon_news",
                       reference_date="2026-01-15", content_md="## Test", title_hash="aabb")
    asset = get_clean_asset(conn, "abc123")
    assert asset is not None
    assert asset["content_md"] == "## Test"
    conn.close()

def test_upsert_and_get_ohlcv():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    upsert_ohlcv(conn, symbol="AAPL", date="2026-01-15",
                 open=150.0, high=155.0, low=149.0, close=152.0, volume=1000000.0)
    bar = get_ohlcv(conn, "AAPL", "2026-01-15")
    assert bar is not None
    assert bar["close"] == 152.0
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/data-core
python -m pytest tests/test_storage.py -v
```
Expected: ImportError or NameError — functions don't exist yet.

- [ ] **Step 3: Implement new storage/sqlite.py**

Rewrite `storage/sqlite.py` with the new schema from spec Section 3.4. The full schema includes `raw_assets`, `clean_assets`, `ohlcv`, `news_alignment`, `attributions`, and `golden_events` tables. Implement `init_db`, `upsert_raw_asset`, `get_raw_asset`, `upsert_clean_asset`, `get_clean_asset`, `upsert_ohlcv`, `get_ohlcv`, and `compute_asset_id` (SHA256 hash). Raw content is zlib-compressed before storage. All datetime fields stored as ISO strings.

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_storage.py -v
```
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/data-core/catalyst_data/storage/sqlite.py packages/data-core/tests/test_storage.py
git commit -m "feat(data-core): implement Bronze/Silver schema with raw_assets, clean_assets, ohlcv tables"
```

### Task 1.2: Rate limiter

**Files:**
- Create: `packages/data-core/catalyst_data/config.py`
- Create: `packages/data-core/catalyst_data/rate_limiter.py`
- Test: `packages/data-core/tests/test_rate_limiter.py`

- [ ] **Step 1: Write failing tests**

```python
# packages/data-core/tests/test_rate_limiter.py
import asyncio
import time
import pytest
from catalyst_data.config import RatePolicy
from catalyst_data.rate_limiter import TokenBucketLimiter, DailyBudgetExhausted

@pytest.mark.asyncio
async def test_rate_limiter_enforces_interval():
    policy = RatePolicy(min_interval_sec=0.1, max_concurrent=1, daily_budget=None)
    limiter = TokenBucketLimiter(policy)
    start = time.monotonic()
    await limiter.acquire()
    await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.1

@pytest.mark.asyncio
async def test_rate_limiter_daily_budget():
    policy = RatePolicy(min_interval_sec=0.0, max_concurrent=5, daily_budget=2)
    limiter = TokenBucketLimiter(policy)
    await limiter.acquire()
    await limiter.acquire()
    with pytest.raises(DailyBudgetExhausted):
        await limiter.acquire()

@pytest.mark.asyncio
async def test_rate_limiter_concurrency():
    policy = RatePolicy(min_interval_sec=0.0, max_concurrent=2, daily_budget=None)
    limiter = TokenBucketLimiter(policy)
    results = []
    async def task(n):
        await limiter.acquire()
        results.append(n)
    await asyncio.gather(task(1), task(2), task(3))
    assert len(results) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_rate_limiter.py -v
```

- [ ] **Step 3: Implement config.py and rate_limiter.py**

`config.py`: Define `RatePolicy` dataclass and `RATE_POLICIES` dict per spec Section 3.2.

`rate_limiter.py`: Implement `TokenBucketLimiter` with `asyncio.Semaphore` for max_concurrent, `asyncio.Lock` + monotonic time for min_interval, and a daily counter that raises `DailyBudgetExhausted`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_rate_limiter.py -v
```

- [ ] **Step 5: Commit**

```bash
git add packages/data-core/catalyst_data/config.py packages/data-core/catalyst_data/rate_limiter.py packages/data-core/tests/test_rate_limiter.py
git commit -m "feat(data-core): add RatePolicy config and TokenBucketLimiter"
```

### Task 1.3: Polygon connector (P0 — OHLCV + news)

**Files:**
- Create: `packages/data-core/catalyst_data/connectors/polygon.py`
- Test: `packages/data-core/tests/test_polygon_connector.py`
- Create: `packages/data-core/tests/fixtures/polygon_ohlcv.json`
- Create: `packages/data-core/tests/fixtures/polygon_news.json`

- [ ] **Step 1: Save test fixtures**

Capture real Polygon API response shapes from the API docs (docs/API_Documentation/polygon_api.md) and save as JSON fixtures. OHLCV response has `results` array with `{o, h, l, c, v, t}` objects. News response has `results` array with `{title, published_utc, article_url, description, tickers}`.

- [ ] **Step 2: Write failing tests**

```python
# packages/data-core/tests/test_polygon_connector.py
import json
import pytest
from pathlib import Path
from catalyst_data.connectors.polygon import create_polygon_fetcher

FIXTURES = Path(__file__).parent / "fixtures"

@pytest.mark.asyncio
async def test_polygon_ohlcv_parses_response(httpx_mock):
    with open(FIXTURES / "polygon_ohlcv.json") as f:
        mock_data = json.load(f)
    httpx_mock.add_response(json=mock_data)
    fetcher = create_polygon_fetcher(api_key="test_key")
    result = await fetcher("AAPL", "ohlcv", "2026-01-15")
    assert result.status == 200
    assert result.data is not None
    assert "results" in result.data or isinstance(result.data, list)

@pytest.mark.asyncio
async def test_polygon_news_parses_response(httpx_mock):
    with open(FIXTURES / "polygon_news.json") as f:
        mock_data = json.load(f)
    httpx_mock.add_response(json=mock_data)
    fetcher = create_polygon_fetcher(api_key="test_key")
    result = await fetcher("AAPL", "news", "2026-01-15")
    assert result.status == 200
    assert result.data is not None
```

Note: Uses `pytest-httpx` for mocking. Add `pytest-httpx` to dev dependencies.

- [ ] **Step 3: Implement polygon.py**

Follow the same factory pattern as existing `fmp.py`: `create_polygon_fetcher(api_key, limiter?, client?) -> fetch(ticker, endpoint, date)`. Two endpoints:
- `ohlcv`: `GET /v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}` — returns daily bars
- `news`: `GET /v2/reference/news?ticker={ticker}&published_utc.gte={date}` — returns articles

The fetcher calls `await limiter.acquire()` before each request (inject limiter via factory).

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_polygon_connector.py -v
```

- [ ] **Step 5: Commit**

```bash
git add packages/data-core/catalyst_data/connectors/polygon.py packages/data-core/tests/test_polygon_connector.py packages/data-core/tests/fixtures/
git commit -m "feat(data-core): add Polygon connector for OHLCV and news"
```

### Task 1.4: FRED connector (clean up existing)

**Files:**
- Create: `packages/data-core/catalyst_data/connectors/fred.py` (new version, existing one was in old folder)
- Test: `packages/data-core/tests/test_fred_connector.py`

- [ ] **Step 1: Write test with fixture**

```python
# packages/data-core/tests/test_fred_connector.py
import pytest
from catalyst_data.connectors.fred import create_fred_fetcher

@pytest.mark.asyncio
async def test_fred_fetches_series(httpx_mock):
    mock_response = {
        "observations": [
            {"date": "2026-01-15", "value": "5.50"},
            {"date": "2026-01-14", "value": "5.50"},
        ]
    }
    httpx_mock.add_response(json=mock_response)
    fetcher = create_fred_fetcher(api_key="test_key")
    result = await fetcher("", "DFF", "2026-01-15")  # FRED doesn't use ticker
    assert result.status == 200
    assert result.data is not None

@pytest.mark.asyncio
async def test_fred_filters_dot_values(httpx_mock):
    mock_response = {
        "observations": [
            {"date": "2026-01-15", "value": "."},
            {"date": "2026-01-14", "value": "5.50"},
        ]
    }
    httpx_mock.add_response(json=mock_response)
    fetcher = create_fred_fetcher(api_key="test_key")
    result = await fetcher("", "DFF", "2026-01-15")
    assert result.status == 200
    # Dot values should be filtered
    obs = result.data.get("observations", [])
    assert all(o["value"] != "." for o in obs)
```

- [ ] **Step 2: Implement fred.py**

`create_fred_fetcher(api_key, limiter?, client?) -> fetch(ticker, endpoint, date)`. Endpoint is a FRED series ID (DFF, DGS10, VIXCLS, etc.). URL: `https://api.stlouisfed.org/fred/series/observations?series_id={endpoint}&api_key={key}&file_type=json&observation_start={date-30d}&observation_end={date}`. Filter out observations where value is `"."`.

- [ ] **Step 3: Run tests, commit**

```bash
python -m pytest tests/test_fred_connector.py -v
git add packages/data-core/catalyst_data/connectors/fred.py packages/data-core/tests/test_fred_connector.py
git commit -m "feat(data-core): add FRED connector with dot-value filtering"
```

### Task 1.5: Update FMP and yfinance connectors

**Files:**
- Modify: `packages/data-core/catalyst_data/connectors/fmp.py` — update imports, inject rate limiter
- Modify: `packages/data-core/catalyst_data/connectors/yfinance_fallback.py` — update imports
- Modify: `packages/data-core/tests/test_fmp_connector.py` — update imports
- Modify: `packages/data-core/tests/test_yfinance_connector.py` — update imports

- [ ] **Step 1: Update imports in all connector files**

In `fmp.py`: change `from data_core.connector_types import FetchResult` → `from catalyst_data.connectors.base import FetchResult`. Replace `semaphore` parameter with `limiter: TokenBucketLimiter` and call `await limiter.acquire()` instead of `async with semaphore`.

In `yfinance_fallback.py`: same import fix.

- [ ] **Step 2: Update test imports and run**

```bash
python -m pytest tests/test_fmp_connector.py tests/test_yfinance_connector.py -v
```

- [ ] **Step 3: Commit**

```bash
git add packages/data-core/catalyst_data/connectors/ packages/data-core/tests/
git commit -m "refactor(data-core): update FMP and yfinance connectors with new imports and rate limiter"
```

### Task 1.6: Pipeline stages (ingest, clean, transform)

**Files:**
- Create: `packages/data-core/catalyst_data/pipeline/stages.py`
- Create: `packages/data-core/catalyst_data/pipeline/ingest.py`
- Create: `packages/data-core/catalyst_data/pipeline/clean.py`
- Create: `packages/data-core/catalyst_data/pipeline/transform.py`
- Test: `packages/data-core/tests/test_pipeline.py`

- [ ] **Step 1: Write failing tests for all 3 stages**

```python
# packages/data-core/tests/test_pipeline.py
from catalyst_data.pipeline.stages import StageResult
from catalyst_data.pipeline.ingest import run_ingest
from catalyst_data.pipeline.clean import run_clean
from catalyst_data.pipeline.transform import run_transform

def test_ingest_rejects_empty_data():
    result = run_ingest(endpoint_data={}, endpoint_statuses={"news": 500})
    assert not result.ok
    assert "No endpoint returned data" in result.error

def test_ingest_accepts_partial_data():
    result = run_ingest(
        endpoint_data={"news": [{"title": "Test"}]},
        endpoint_statuses={"news": 200, "ohlcv": 500}
    )
    assert result.ok

def test_clean_deduplicates_news_by_title():
    raw = {"articles": [
        {"title": "Apple Beats Earnings", "published_utc": "2026-01-15T10:00:00Z"},
        {"title": "Apple beats earnings!", "published_utc": "2026-01-15T10:30:00Z"},
        {"title": "Different Story", "published_utc": "2026-01-15T11:00:00Z"},
    ]}
    result = run_clean(raw, source_type="polygon_news")
    assert result.ok
    assert len(result.data) == 2  # deduped by normalized title + time window

def test_clean_merges_financial_endpoints():
    raw = {
        "income_statement": [{"revenue": 100}],
        "balance_sheet": [{"assets": 200}],
    }
    result = run_clean(raw, source_type="fmp_fundamentals")
    assert result.ok
    assert "income_statement" in result.data
    assert "balance_sheet" in result.data

def test_transform_news_to_markdown():
    articles = [{"title": "Apple Beats Earnings", "source": "Reuters",
                 "published_utc": "2026-01-15T10:00:00Z", "url": "https://example.com",
                 "content": "Apple reported record revenue."}]
    result = run_transform(articles, source_type="polygon_news")
    assert result.ok
    assert "## AAPL" in result.data or "Apple Beats Earnings" in result.data
    assert "*Source:" in result.data
    assert "## References" in result.data

def test_transform_financial_to_markdown_table():
    data = {"income_statement": {"revenue": 100, "net_income": 50}}
    result = run_transform(data, source_type="fmp_fundamentals")
    assert result.ok
    assert "| Metric" in result.data or "| Field" in result.data
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_pipeline.py -v
```

- [ ] **Step 3: Implement stages.py**

`StageResult` dataclass (stage name, ok bool, data, error, latency_ms) — same as existing but moved to new location.

- [ ] **Step 4: Implement ingest.py**

`run_ingest(endpoint_data, endpoint_statuses) -> StageResult` — validates at least one endpoint has data.

- [ ] **Step 5: Implement clean.py**

`run_clean(raw_data, source_type) -> StageResult` — normalizes data by source type. For news sources, runs cross-source dedup using `compute_dedup_fingerprint(title, published_utc)` from spec Section 3.5. For financial sources, merges multi-endpoint data.

- [ ] **Step 6: Implement transform.py**

`run_transform(cleaned_data, source_type) -> StageResult` — converts to structured Markdown. Uses existing `transmuter.py` functions internally. Adds the consistent `*Source: ... | datetime ET | Category: ...*` header for news articles.

- [ ] **Step 7: Run tests, commit**

```bash
python -m pytest tests/test_pipeline.py -v
git add packages/data-core/catalyst_data/pipeline/ packages/data-core/tests/test_pipeline.py
git commit -m "feat(data-core): implement 3-stage pipeline (ingest, clean with dedup, transform)"
```

### Task 1.7: Cross-source dedup module

**Files:**
- Create: `packages/data-core/catalyst_data/dedup/hard.py`
- Test: `packages/data-core/tests/test_dedup.py`

- [ ] **Step 1: Write failing tests**

```python
# packages/data-core/tests/test_dedup.py
from catalyst_data.dedup.hard import compute_dedup_fingerprint, deduplicate_articles

def test_fingerprint_ignores_punctuation():
    fp1 = compute_dedup_fingerprint("Apple Beats Earnings!", "2026-01-15T10:00:00Z")
    fp2 = compute_dedup_fingerprint("Apple beats earnings", "2026-01-15T10:30:00Z")
    assert fp1 == fp2  # same 2-hour window, normalized title matches

def test_fingerprint_differs_across_time_windows():
    fp1 = compute_dedup_fingerprint("Apple Beats Earnings", "2026-01-15T10:00:00Z")
    fp2 = compute_dedup_fingerprint("Apple Beats Earnings", "2026-01-15T14:00:00Z")
    assert fp1 != fp2  # different 2-hour windows

def test_deduplicate_articles_removes_dupes():
    articles = [
        {"title": "Apple Beats Earnings!", "published_utc": "2026-01-15T10:00:00Z"},
        {"title": "Apple beats earnings", "published_utc": "2026-01-15T10:30:00Z"},
        {"title": "Different Story", "published_utc": "2026-01-15T10:00:00Z"},
    ]
    result = deduplicate_articles(articles)
    assert len(result) == 2
```

- [ ] **Step 2: Implement hard.py**

`compute_dedup_fingerprint(title, published_utc) -> str` per spec Section 3.5. `deduplicate_articles(articles) -> list[dict]` filters duplicates by fingerprint.

- [ ] **Step 3: Run tests, commit**

```bash
python -m pytest tests/test_dedup.py -v
git add packages/data-core/catalyst_data/dedup/ packages/data-core/tests/test_dedup.py
git commit -m "feat(data-core): add cross-source dedup with title+time fingerprint"
```

### Task 1.8: News-to-trading-day alignment

**Files:**
- Create: `packages/data-core/catalyst_data/pipeline/align.py`
- Test: `packages/data-core/tests/test_align.py`

- [ ] **Step 1: Write failing tests**

```python
# packages/data-core/tests/test_align.py
from catalyst_data.pipeline.align import map_to_trade_date, compute_forward_returns

def test_premarket_news_maps_to_same_day():
    trading_days = ["2026-01-14", "2026-01-15", "2026-01-16"]
    result = map_to_trade_date("2026-01-15T08:00:00-05:00", trading_days)
    assert result == "2026-01-15"

def test_afterhours_news_maps_to_next_day():
    trading_days = ["2026-01-14", "2026-01-15", "2026-01-16"]
    result = map_to_trade_date("2026-01-15T18:00:00-05:00", trading_days)
    assert result == "2026-01-16"

def test_weekend_news_maps_to_monday():
    trading_days = ["2026-01-16", "2026-01-20", "2026-01-21"]  # Fri, Mon, Tue
    result = map_to_trade_date("2026-01-17T12:00:00-05:00", trading_days)  # Saturday
    assert result == "2026-01-20"  # next Monday

def test_forward_returns():
    closes = {"2026-01-14": 150.0, "2026-01-15": 148.0, "2026-01-16": 152.0,
              "2026-01-17": 151.0, "2026-01-20": 155.0, "2026-01-21": 153.0}
    dates_sorted = sorted(closes.keys())
    returns = compute_forward_returns("2026-01-15", closes, dates_sorted)
    assert returns["ret_t0"] == pytest.approx((148.0 - 150.0) / 150.0, abs=1e-6)
    assert returns["ret_t1"] == pytest.approx((152.0 - 148.0) / 148.0, abs=1e-6)
```

- [ ] **Step 2: Implement align.py**

`map_to_trade_date(published_utc, trading_days) -> str|None` — per spec Stage 4 rules. `compute_forward_returns(trade_date, closes_dict, dates_sorted) -> dict` — T+0, T+1, T+3, T+5 returns. Adapted from PokieTicker's `alignment.py`.

- [ ] **Step 3: Run tests, commit**

```bash
python -m pytest tests/test_align.py -v
git add packages/data-core/catalyst_data/pipeline/align.py packages/data-core/tests/test_align.py
git commit -m "feat(data-core): add news-to-trading-day alignment with forward returns"
```

### Task 1.9: Orchestrator (ties it all together)

**Files:**
- Create: `packages/data-core/catalyst_data/orchestrator.py`
- Test: `packages/data-core/tests/test_orchestrator.py`

- [ ] **Step 1: Write integration test**

```python
# packages/data-core/tests/test_orchestrator.py
import sqlite3
import pytest
from catalyst_data.storage.sqlite import init_db
from catalyst_data.orchestrator import process_request
from catalyst_data.connectors.base import FetchResult

async def mock_fetch(ticker, endpoint, date):
    if endpoint == "news":
        return FetchResult(status=200, data={"results": [
            {"title": "Test Article", "published_utc": "2026-01-15T10:00:00Z",
             "article_url": "https://example.com", "description": "Test content"}
        ]}, latency_ms=10.0, source_label="polygon:news")
    return FetchResult(status=200, data=[{"revenue": 100}],
                       latency_ms=10.0, source_label=f"fmp:{endpoint}")

@pytest.mark.asyncio
async def test_process_request_stores_bronze_and_silver():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    results = await process_request(
        ticker="AAPL", date="2026-01-15", sources=["polygon_news"],
        conn=conn, primary_fetch_fn=mock_fetch,
    )
    assert len(results) >= 1
    # Check Bronze
    raw = conn.execute("SELECT * FROM raw_assets").fetchall()
    assert len(raw) >= 1
    # Check Silver
    clean = conn.execute("SELECT * FROM clean_assets").fetchall()
    assert len(clean) >= 1
    conn.close()
```

- [ ] **Step 2: Implement orchestrator.py**

The new orchestrator calls: fetch → `run_ingest` → store in `raw_assets` → `run_clean` → `run_transform` → store in `clean_assets`. Uses `source_mapping.py` to map logical sources to physical endpoints. Injects rate limiter per provider.

- [ ] **Step 3: Run tests, commit**

```bash
python -m pytest tests/test_orchestrator.py -v
git add packages/data-core/catalyst_data/orchestrator.py packages/data-core/tests/test_orchestrator.py
git commit -m "feat(data-core): add orchestrator connecting fetch → pipeline → storage"
```

### Task 1.10: Smoke test script

**Files:**
- Create: `packages/data-core/scripts/smoke_test.py`

- [ ] **Step 1: Write the script**

CLI script: `python -m catalyst_data.scripts.smoke_test --ticker AAPL --days 3`. Creates a dev database, fetches 3 days of data for one ticker from all available providers (based on which API keys are in `.env`), runs full pipeline, prints summary of what was stored. Writes debug artifacts to `data/smoke_artifacts/`.

- [ ] **Step 2: Test manually with real API key**

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/data-core
python -m catalyst_data.scripts.smoke_test --ticker AAPL --days 3
```

Check that `data/dev_assets.db` contains rows in both `raw_assets` and `clean_assets`.

- [ ] **Step 3: Commit**

```bash
git add packages/data-core/scripts/smoke_test.py
git commit -m "feat(data-core): add smoke test script for pipeline validation"
```

---

## Phase 2: Eval Harness (Days 4-5)

### Task 2.1: Scaffold eval package

**Files:**
- Create: `packages/eval/pyproject.toml`
- Create: `packages/eval/catalyst_eval/__init__.py`
- Create: `packages/eval/catalyst_eval/schema/golden_event.py`
- Create: `packages/eval/catalyst_eval/schema/result.py`
- Test: `packages/eval/tests/test_schema.py`

- [ ] **Step 1: Create pyproject.toml**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "catalyst-eval"
version = "0.1.0"
description = "Financial AI agent evaluation framework with domain-specific attribution metrics"
requires-python = ">=3.11"
license = "MIT"
authors = [{ name = "Yiannis Chen" }]

dependencies = [
    "pydantic>=2.0",
    "numpy>=1.24",
]

[project.optional-dependencies]
ragas = ["ragas>=0.1", "langchain-core>=0.2"]
dev = ["pytest>=8.0"]

[tool.hatch.build.targets.wheel]
packages = ["catalyst_eval"]
```

- [ ] **Step 2: Implement schema types**

`golden_event.py`: `CauseCategory` enum, `Cause` model (text, category, weight, temporal_anchor, evidence_ids), `GoldenEvent` model (id, ticker, trade_date, price_move_pct, causes). All as Pydantic BaseModel.

`result.py`: `PredictedCause` model (text, category, confidence, evidence_ids, direction), `AttributionResult` model (ticker, trade_date, causes, summary, retrieved_chunks, cost_breakdown, total_cost_usd, total_tokens). This is the contract any agent must return.

- [ ] **Step 3: Write and run schema tests**

```python
# packages/eval/tests/test_schema.py
from catalyst_eval.schema.golden_event import GoldenEvent, Cause, CauseCategory
from catalyst_eval.schema.result import AttributionResult, PredictedCause

def test_golden_event_validates():
    event = GoldenEvent(
        id="test_001", ticker="AAPL", trade_date="2026-01-15",
        price_move_pct=-4.2,
        causes=[Cause(text="Earnings miss", category=CauseCategory.EARNINGS,
                       weight=0.7, temporal_anchor="after-hours", evidence_ids=["a1"])]
    )
    assert event.ticker == "AAPL"
    assert event.causes[0].weight == 0.7

def test_attribution_result_validates():
    result = AttributionResult(
        ticker="AAPL", trade_date="2026-01-15",
        causes=[PredictedCause(text="Earnings miss", category="earnings",
                                confidence=0.8, evidence_ids=["c1"], direction="negative")],
        summary="AAPL dropped due to earnings miss [c1].",
        retrieved_chunks=["chunk1"],
        cost_breakdown=[], total_cost_usd=0.03, total_tokens=7500
    )
    assert result.causes[0].confidence == 0.8
```

- [ ] **Step 4: Commit**

```bash
git add packages/eval/
git commit -m "feat(eval): scaffold catalyst-eval package with schema types"
```

### Task 2.2: Implement 5 metrics

**Files:**
- Create: `packages/eval/catalyst_eval/metrics/base.py`
- Create: `packages/eval/catalyst_eval/metrics/attribution_f1.py`
- Create: `packages/eval/catalyst_eval/metrics/category_accuracy.py`
- Create: `packages/eval/catalyst_eval/metrics/grounding_rate.py`
- Create: `packages/eval/catalyst_eval/metrics/temporal_precision.py`
- Create: `packages/eval/catalyst_eval/metrics/confidence_calibration.py`
- Test: `packages/eval/tests/test_metrics.py`

- [ ] **Step 1: Define BaseMetric protocol**

```python
# packages/eval/catalyst_eval/metrics/base.py
from typing import Protocol
from catalyst_eval.schema.golden_event import GoldenEvent
from catalyst_eval.schema.result import AttributionResult

class BaseMetric(Protocol):
    name: str
    def compute(self, predicted: AttributionResult, golden: GoldenEvent) -> float: ...
```

- [ ] **Step 2: Write failing tests for each metric**

```python
# packages/eval/tests/test_metrics.py
from catalyst_eval.schema.golden_event import GoldenEvent, Cause, CauseCategory
from catalyst_eval.schema.result import AttributionResult, PredictedCause
from catalyst_eval.metrics.attribution_f1 import AttributionF1
from catalyst_eval.metrics.category_accuracy import CategoryAccuracy
from catalyst_eval.metrics.temporal_precision import TemporalPrecision

GOLDEN = GoldenEvent(
    id="t1", ticker="AAPL", trade_date="2026-01-15", price_move_pct=-4.2,
    causes=[
        Cause(text="China export ban on H20 chips", category=CauseCategory.GEOPOLITICAL,
              weight=0.6, temporal_anchor="pre-market", evidence_ids=[]),
        Cause(text="Sector selloff", category=CauseCategory.SECTOR,
              weight=0.3, temporal_anchor="intraday", evidence_ids=[]),
    ]
)

PREDICTED_GOOD = AttributionResult(
    ticker="AAPL", trade_date="2026-01-15",
    causes=[
        PredictedCause(text="China chip export restrictions expanded", category="geopolitical",
                        confidence=0.7, evidence_ids=["c1"], direction="negative"),
        PredictedCause(text="Broad semiconductor selloff", category="sector",
                        confidence=0.2, evidence_ids=["c2"], direction="negative"),
    ],
    summary="...", retrieved_chunks=["c1", "c2"],
    cost_breakdown=[], total_cost_usd=0.03, total_tokens=7500
)

PREDICTED_BAD = AttributionResult(
    ticker="AAPL", trade_date="2026-01-15",
    causes=[
        PredictedCause(text="Earnings disappointment", category="earnings",
                        confidence=0.9, evidence_ids=["c1"], direction="negative"),
    ],
    summary="...", retrieved_chunks=["c1"],
    cost_breakdown=[], total_cost_usd=0.02, total_tokens=5000
)

def test_attribution_f1_good_prediction():
    metric = AttributionF1()
    score = metric.compute(PREDICTED_GOOD, GOLDEN)
    assert score > 0.5  # should match both causes semantically

def test_attribution_f1_bad_prediction():
    metric = AttributionF1()
    score = metric.compute(PREDICTED_BAD, GOLDEN)
    assert score < 0.3  # "earnings" doesn't match any golden cause

def test_category_accuracy_correct():
    metric = CategoryAccuracy()
    score = metric.compute(PREDICTED_GOOD, GOLDEN)
    assert score >= 0.5  # both categories match

def test_category_accuracy_wrong():
    metric = CategoryAccuracy()
    score = metric.compute(PREDICTED_BAD, GOLDEN)
    assert score == 0.0  # "earnings" doesn't match "geopolitical" or "sector"

def test_temporal_precision():
    metric = TemporalPrecision()
    score = metric.compute(PREDICTED_GOOD, GOLDEN)
    # trade_date matches → score should be 1.0
    assert score == 1.0
```

- [ ] **Step 3: Implement all 5 metrics**

- `attribution_f1.py`: Semantic matching between predicted and golden cause texts. Use simple string overlap (Jaccard on word tokens) as a lightweight proxy — full embedding-based matching deferred to when bge-m3 is available. Compute precision, recall, F1.
- `category_accuracy.py`: For matched cause pairs, check if CauseCategory matches exactly.
- `grounding_rate.py`: For each sentence in summary, check if any retrieved_chunk contains overlapping content. Simple word overlap ratio. Full RAGAS faithfulness wrapping is an optional enhancement.
- `temporal_precision.py`: Check if predicted trade_date matches golden trade_date. Binary.
- `confidence_calibration.py`: Group predictions by confidence bucket, compute accuracy per bucket. Returns mean absolute calibration error.

- [ ] **Step 4: Run tests, commit**

```bash
cd /Users/yiannischen/Desktop/Catalyst/packages/eval
python -m pytest tests/test_metrics.py -v
git add packages/eval/catalyst_eval/metrics/ packages/eval/tests/test_metrics.py
git commit -m "feat(eval): implement 5 financial attribution metrics"
```

### Task 2.3: Harness runner and experiment comparator

**Files:**
- Create: `packages/eval/catalyst_eval/harness/runner.py`
- Create: `packages/eval/catalyst_eval/harness/experiment.py`
- Create: `packages/eval/catalyst_eval/reports/markdown.py`
- Test: `packages/eval/tests/test_harness.py`

- [ ] **Step 1: Write failing tests**

```python
# packages/eval/tests/test_harness.py
from catalyst_eval.harness.runner import evaluate
from catalyst_eval.harness.experiment import compare
from catalyst_eval.metrics.attribution_f1 import AttributionF1
from catalyst_eval.metrics.category_accuracy import CategoryAccuracy
from catalyst_eval.schema.golden_event import GoldenEvent, Cause, CauseCategory
from catalyst_eval.schema.result import AttributionResult, PredictedCause

def mock_good_agent(ticker, date):
    return AttributionResult(
        ticker=ticker, trade_date=date,
        causes=[PredictedCause(text="China chip ban", category="geopolitical",
                                confidence=0.7, evidence_ids=["c1"], direction="negative")],
        summary="Test", retrieved_chunks=["c1"],
        cost_breakdown=[{"node": "judge", "input_tokens": 3000, "output_tokens": 500,
                         "model_id": "test", "cost_usd": 0.02}],
        total_cost_usd=0.02, total_tokens=3500
    )

def mock_bad_agent(ticker, date):
    return AttributionResult(
        ticker=ticker, trade_date=date,
        causes=[PredictedCause(text="Random guess", category="technical",
                                confidence=0.9, evidence_ids=[], direction="negative")],
        summary="Test", retrieved_chunks=[],
        cost_breakdown=[], total_cost_usd=0.01, total_tokens=2000
    )

GOLDEN_SET = [GoldenEvent(
    id="t1", ticker="AAPL", trade_date="2026-01-15", price_move_pct=-4.2,
    causes=[Cause(text="China export ban", category=CauseCategory.GEOPOLITICAL,
                   weight=0.7, temporal_anchor="pre-market", evidence_ids=[])]
)]

def test_evaluate_returns_scores():
    report = evaluate(predict_fn=mock_good_agent, golden_set=GOLDEN_SET,
                      metrics=[AttributionF1(), CategoryAccuracy()])
    assert "attribution_f1" in report.scores
    assert "category_accuracy" in report.scores
    assert report.scores["attribution_f1"] > 0

def test_compare_returns_comparison():
    comparison = compare(
        configs={"good": mock_good_agent, "bad": mock_bad_agent},
        golden_set=GOLDEN_SET,
        metrics=[AttributionF1()],
    )
    assert "good" in comparison.results
    assert "bad" in comparison.results
    assert comparison.results["good"]["attribution_f1"] > comparison.results["bad"]["attribution_f1"]
```

- [ ] **Step 2: Implement runner.py**

`evaluate(predict_fn, golden_set, metrics) -> EvalReport`. Iterates over golden set, calls predict_fn for each, runs all metrics, returns averaged scores.

- [ ] **Step 3: Implement experiment.py**

`compare(configs: dict[str, Callable], golden_set, metrics) -> ComparisonReport`. Runs `evaluate` for each config, aggregates into comparison dict. Also tracks avg cost/tokens per config.

- [ ] **Step 4: Implement reports/markdown.py**

`ComparisonReport.to_markdown() -> str`. Generates the comparison table from spec Section 5.6.

- [ ] **Step 5: Run tests, commit**

```bash
python -m pytest tests/test_harness.py -v
git add packages/eval/catalyst_eval/harness/ packages/eval/catalyst_eval/reports/ packages/eval/tests/test_harness.py
git commit -m "feat(eval): add harness runner, experiment comparator, and markdown reports"
```

### Task 2.4: Golden set (initial 5 events)

**Files:**
- Create: `packages/eval/golden_set/v1.jsonl`
- Create: `packages/eval/golden_set/README.md`

- [ ] **Step 1: Create initial golden set**

Start with 5 well-known market events (expand to 15 later). Research real events and annotate causes. Example format in JSONL:

```jsonl
{"id":"g001","ticker":"NVDA","trade_date":"2025-05-28","price_move_pct":-5.7,"causes":[{"text":"China expanded export restrictions to H20 chips","category":"geopolitical","weight":0.6,"temporal_anchor":"pre-market","evidence_ids":[]},{"text":"Broader semiconductor sector selloff","category":"sector","weight":0.3,"temporal_anchor":"intraday","evidence_ids":[]}]}
```

Note: The user (Yiannis) must research and verify the actual events. The implementer should scaffold the file format and provide 2-3 example entries, then the user fills in the rest.

- [ ] **Step 2: Write README with annotation guidelines**

- [ ] **Step 3: Commit**

```bash
git add packages/eval/golden_set/
git commit -m "feat(eval): add initial golden set with annotation guidelines"
```

---

## Phase 3: Agent Workflow (Days 6-7)

### Task 3.1: Scaffold agent package with LangGraph state

**Files:**
- Create: `catalyst/agents/__init__.py`
- Create: `catalyst/agents/state.py`
- Create: `catalyst/agents/cost_tracker.py`
- Test: `catalyst/agents/tests/test_state.py`

- [ ] **Step 1: Implement AttributionState and CostTracker**

`state.py`: TypedDict matching spec Section 4.2 exactly.

`cost_tracker.py`: `MODEL_PRICING` dict + `track_cost(state, node, response)` function per spec.

- [ ] **Step 2: Write tests**

```python
# catalyst/agents/tests/test_state.py
from catalyst.agents.cost_tracker import track_cost, MODEL_PRICING

class MockUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = input_tokens + output_tokens

class MockResponse:
    def __init__(self):
        self.usage = MockUsage(3000, 500)

def test_track_cost_appends_breakdown():
    state = {"model_id": "claude-sonnet-4-20250514",
             "cost_breakdown": [], "total_cost_usd": 0.0, "total_tokens": 0}
    track_cost(state, "critic", MockResponse())
    assert len(state["cost_breakdown"]) == 1
    assert state["cost_breakdown"][0]["node"] == "critic"
    assert state["total_cost_usd"] > 0
    assert state["total_tokens"] == 3500
```

- [ ] **Step 3: Run tests, commit**

```bash
python -m pytest catalyst/agents/tests/test_state.py -v
git add catalyst/agents/
git commit -m "feat(agents): add AttributionState and CostTracker"
```

### Task 3.2: LanceDB Gold layer (build_index + hybrid search)

**Files:**
- Create: `packages/data-core/catalyst_data/storage/lancedb.py`
- Create: `packages/data-core/scripts/build_index.py`
- Test: `packages/data-core/tests/test_lancedb.py`

- [ ] **Step 1: Implement lancedb.py**

Functions: `build_index(db_path, lancedb_path, embedding_model)` — reads clean_assets, chunks, embeds with bge-m3, upserts to LanceDB. `hybrid_search(table, query, ticker, date_range, top_k=20)` — BM25 + vector parallel search, merged with RRF per spec Section 6.1. `reciprocal_rank_fusion(*result_lists, k=60)` — per ADR-002.

- [ ] **Step 2: Write tests**

Test RRF merge logic with mock data (doesn't need actual embeddings). Test that `hybrid_search` combines BM25 and vector results.

- [ ] **Step 3: Write build_index.py script**

CLI: `python -m catalyst_data.scripts.build_index --db data/dev_assets.db --lancedb data/lancedb/`

- [ ] **Step 4: Commit**

```bash
git add packages/data-core/catalyst_data/storage/lancedb.py packages/data-core/scripts/build_index.py packages/data-core/tests/test_lancedb.py
git commit -m "feat(data-core): add LanceDB Gold layer with hybrid BM25+vector search and RRF"
```

### Task 3.3: Miner node (retrieval + reranking)

**Files:**
- Create: `catalyst/agents/nodes/miner.py`
- Test: `catalyst/agents/tests/test_miner.py`

- [ ] **Step 1: Implement miner.py**

`miner(state: AttributionState) -> dict` — calls `hybrid_search` from lancedb.py, then reranks with bge-reranker-v2 (FlagReranker). Returns `{"retrieved_chunks": [...], "reranked_chunks": [...]}`.

- [ ] **Step 2: Write test with mock LanceDB**

Mock the hybrid_search to return fake chunks. Test that miner populates `reranked_chunks` with top-8.

- [ ] **Step 3: Commit**

```bash
git add catalyst/agents/nodes/miner.py catalyst/agents/tests/test_miner.py
git commit -m "feat(agents): add Miner node with hybrid retrieval and reranking"
```

### Task 3.4: Critic node + insufficient evidence handler

**Files:**
- Create: `catalyst/agents/nodes/critic.py`
- Create: `catalyst/agents/prompts/critic.md`
- Test: `catalyst/agents/tests/test_critic.py`

- [ ] **Step 1: Write critic.md prompt**

Per spec Section 4.3 — the full Critic prompt that grades each chunk with relevance, category, temporal_match, reasoning.

- [ ] **Step 2: Implement critic.py**

`critic(state: AttributionState) -> dict` — formats prompt with state data, calls LLM, parses JSON response, filters chunks with relevance > 0.5, calls `track_cost`. Returns `{"graded_evidence": [...], "critic_reasoning": "..."}`.

`insufficient_handler(state: AttributionState) -> dict` — returns canned AttributionResult per spec Section 4.3.

- [ ] **Step 3: Write test (mock LLM)**

Mock the LLM call to return a known JSON grading. Test that filtering works, that insufficient_handler returns correct canned response.

- [ ] **Step 4: Commit**

```bash
git add catalyst/agents/nodes/critic.py catalyst/agents/prompts/critic.md catalyst/agents/tests/test_critic.py
git commit -m "feat(agents): add Critic node with evidence grading and insufficient evidence fallback"
```

### Task 3.5: Judge node

**Files:**
- Create: `catalyst/agents/nodes/judge.py`
- Create: `catalyst/agents/prompts/judge.md`
- Test: `catalyst/agents/tests/test_judge.py`

- [ ] **Step 1: Write judge.md prompt**

Per spec Section 4.3 — synthesize causes with confidence scores, citations, self-grounding check.

- [ ] **Step 2: Implement judge.py**

`judge(state: AttributionState) -> dict` — formats prompt, calls LLM, parses JSON, calls `track_cost`. Returns `{"causes": [...], "summary_md": "...", "grounding_rate": float}`.

- [ ] **Step 3: Write test (mock LLM), commit**

```bash
git add catalyst/agents/nodes/judge.py catalyst/agents/prompts/judge.md catalyst/agents/tests/test_judge.py
git commit -m "feat(agents): add Judge node with attribution synthesis"
```

### Task 3.6: LangGraph graph assembly

**Files:**
- Create: `catalyst/agents/graph.py`
- Test: `catalyst/agents/tests/test_graph.py`

- [ ] **Step 1: Implement graph.py**

`build_attribution_graph(use_critic=True) -> CompiledGraph` — per spec Section 4.4. Wire Miner → Critic → Judge with conditional edge (`route_after_critic`). Also `build_attribution_graph(use_critic=False)` for baseline.

- [ ] **Step 2: Write integration test**

Test both graph variants with fully mocked nodes. Verify that:
- MCJ graph calls all 3 nodes in order
- Baseline graph skips Critic
- When Critic returns empty graded_evidence, insufficient_handler is called instead of Judge

- [ ] **Step 3: Commit**

```bash
git add catalyst/agents/graph.py catalyst/agents/tests/test_graph.py
git commit -m "feat(agents): assemble LangGraph MCJ workflow with conditional edges"
```

### Task 3.7: Catalyst adapter for eval + run experiments

**Files:**
- Create: `catalyst/adapters/catalyst_adapter.py`
- Create: `scripts/run_experiments.py`

- [ ] **Step 1: Implement catalyst_adapter.py**

Wraps the LangGraph graph into a `predict_fn(ticker, date) -> AttributionResult` that catalyst-eval's harness can call.

```python
def make_catalyst_predict(graph, db_path, lancedb_path, model_id):
    def predict(ticker: str, date: str) -> AttributionResult:
        state = {"ticker": ticker, "trade_date": date, ...}
        result = graph.invoke(state)
        return AttributionResult(
            ticker=result["ticker"], trade_date=result["trade_date"],
            causes=[PredictedCause(**c) for c in result["causes"]],
            summary=result["summary_md"],
            retrieved_chunks=[c["content_md"] for c in result.get("reranked_chunks", [])],
            cost_breakdown=result["cost_breakdown"],
            total_cost_usd=result["total_cost_usd"],
            total_tokens=result["total_tokens"],
        )
    return predict
```

- [ ] **Step 2: Implement run_experiments.py**

Runs experiments E1-E3 from spec Section 5.5:
- E1: Raw vs Markdown (toggle transform stage)
- E2: Single Agent vs MCJ (toggle use_critic)
- E3: Hybrid vs Vector-Only (toggle BM25 in miner)

Each experiment calls `compare()` from catalyst_eval, generates markdown report, saves to `data/eval_reports/`.

```bash
python scripts/run_experiments.py --golden-set packages/eval/golden_set/v1.jsonl --output data/eval_reports/
```

- [ ] **Step 3: Commit**

```bash
git add catalyst/adapters/ scripts/run_experiments.py
git commit -m "feat: add catalyst adapter and experiment runner for E1-E3"
```

### Task 3.8: Merge to main for midterm

- [ ] **Step 1: Run full test suite**

```bash
cd /Users/yiannischen/Desktop/Catalyst
python -m pytest packages/data-core/tests/ packages/eval/tests/ catalyst/agents/tests/ -v
```

- [ ] **Step 2: Merge feature branch to main**

```bash
git checkout main
git merge feat/data-ingestion --no-ff -m "feat: Catalyst v0.1-midterm — data pipeline + eval harness + MCJ agent"
git tag v0.1-midterm
```

- [ ] **Step 3: Verify**

```bash
git log --oneline -10
```

---

## File Summary

| Phase | New files | Modified files |
|---|---|---|
| Phase 0 | `pyproject.toml` | All existing `.py` (import rename) |
| Phase 1 | `config.py`, `rate_limiter.py`, `connectors/polygon.py`, `connectors/fred.py`, `pipeline/{stages,ingest,clean,transform,align}.py`, `dedup/hard.py`, `storage/sqlite.py`, `orchestrator.py`, `scripts/smoke_test.py` | `connectors/fmp.py`, `connectors/yfinance_fallback.py`, all tests |
| Phase 2 | `catalyst_eval/` entire package (schema, 5 metrics, harness, reports), `golden_set/v1.jsonl` | None |
| Phase 3 | `catalyst/agents/` entire package (state, cost_tracker, 3 nodes, graph, prompts), `catalyst/adapters/`, `scripts/run_experiments.py`, `storage/lancedb.py`, `scripts/build_index.py` | None |
