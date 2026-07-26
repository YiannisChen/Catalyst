# ADR-001: SQLite over PostgreSQL

**Status:** Accepted
**Date:** 2026-04-02
**Decision:** Use SQLite (WAL mode) as the sole relational database for all Catalyst packages.

Current migration ownership and frozen-write protections are binding in `docs/plans/2026-07-21-b2-b7-technical-contracts.md`.

## Context

Catalyst needs a relational store for raw data (Bronze), cleaned data (Silver), OHLCV prices, news alignment, attributions, and golden set events. The system is a single-user research tool used for academic research and portfolio demonstration.

## Options Considered

### Option A: PostgreSQL
- Industry standard for production systems
- Full ACID, concurrent writes, native JSON operators
- Requires server process (docker-compose or system service)
- Users must install Postgres before running Catalyst
- Cannot distribute as a single file

### Option B: SQLite (WAL mode)
- Zero-config, embedded, single file
- WAL mode gives concurrent read/write for single-writer workloads
- Portable: entire database distributable as `assets.db.zst` via GitHub Release
- Users clone repo + download snapshot = running in minutes
- No server process, no Docker dependency

### Option C: DuckDB
- Excellent for analytical queries (column-oriented)
- Weaker for concurrent write patterns during ingestion
- Smaller ecosystem than SQLite
- Less battle-tested for application storage

## Decision

**SQLite with WAL mode.** Business justification:

1. **Deployment friction:** PostgreSQL requires a running server. For an open-source package that anyone should be able to `pip install` and use, this is an unnecessary barrier. Teacher Mac: "What business pain does Postgres solve here?" — none, it's single-user.

2. **Portability:** The entire dataset is one file. Publish as a GitHub Release asset, users download and query immediately. This is impossible with PostgreSQL.

3. **Sufficient for workload:** Single writer (the ingestion pipeline), multiple readers (API, agent, eval). WAL mode handles this perfectly. We never need multi-tenant concurrent writes.

4. **Pragmas for reliability:**
   ```sql
   PRAGMA journal_mode=WAL;
   PRAGMA synchronous=NORMAL;
   PRAGMA busy_timeout=5000;
   PRAGMA foreign_keys=ON;
   ```

## Consequences

- No native full-text search as powerful as Postgres `tsvector` — mitigated by LanceDB BM25 for the Gold layer
- No native JSON operators — JSON stored as TEXT, parsed in Python
- Single-writer limitation — acceptable for single-user tool
- If Catalyst ever becomes multi-tenant, this decision must be revisited

## References

- PokieTicker uses identical pattern: SQLite + WAL, single-user financial tool
- crawl4ai uses aiosqlite for async SQLite with similar pragmas
- valuecell uses SQLAlchemy + aiosqlite with StaticPool
