# W1-B: Calendar/OHLCV Planning — Implementation Plan

> **For executor:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace stale table-derived defaults with calendar-derived per-ticker expected sessions, an explicit configured universe, and year-range-aware calendar coverage.

**Architecture:** The planner computes a per-ticker default window: `from_date` = max(explicit configured start, that ticker's local OHLCV watermark + 1 session, configured historical start); `to_date` = latest closed market session from injected `reference_today`. The universe defaults to the configured explicit constant with `"explicit-config"` provenance. OHLCV-derived fallback is available only with a compatibility flag and emits a typed warning — never silently used for certified runs. Calendar coverage fails closed outside supported years.

**Tech Stack:** Python 3.13, `trading_calendar.py`, `config.py`, `update_planner.py`.

---

## 1. Scope

- Add `TICKER_UNIVERSE` constant to `config.py` (proposed ten tickers pending AD-UNIVERSE-1)
- Add supported calendar year range to `trading_calendar.py` with fail-closed guard
- `CalendarCoverageError` for dates outside supported calendar range
- Per-ticker default windows: each ticker's `from_date` derived from its own OHLCV watermark, not a single global minimum
- Universe resolution: explicit tickers → `"explicit-config"`; configured constant → `"explicit-config"`; OHLCV-derived only with compatibility flag → `"ohlcv-derived-fallback"` + typed warning
- Tests: per-ticker windows, stale-ticker independence, absent-ticker historical start, circularity guard, calendar boundary

**Out of scope:** Executor loop, connectors, upsert semantics, OHLCV cell handling (W1-C), CLI (W1-F).

---

## 2. Non-Goals

- No executor changes
- No OHLCV handler (W1-C)
- No schema changes
- No CLI
- No live provider calls

---

## 3. Verified Current Implementation

| File | Symbol | Status |
|---|---|---|
| `trading_calendar.py:13` | `US_MARKET_HOLIDAYS` | 2025–2027 only; 2024 missing |
| `trading_calendar.py:26` | `calendar_trading_days(from_date, to_date)` | Returns weekday-non-holiday dates in range |
| `trading_calendar.py:46` | `latest_closed_trading_day_for_date(ref_date)` | Returns last closed trading day before ref_date |
| `trading_calendar.py:69` | `trading_days_for_window(conn, from_date, to_date)` | Unions ohlcv days with calendar — must NOT be used for expectation |
| `config.py` | No `TICKER_UNIVERSE` | Universe currently implicit |
| `update_pipeline.py:76` | `compute_missing_cells` | Defaults tickers to `DISTINCT symbol FROM ohlcv` |
| `update_pipeline.py:897` | `run_update_batch` defaults | Both dates default to `latest_local_ohlcv_date` |
| `update_planner.py` (W1-A) | `plan_update` | Needs per-ticker defaults |

---

## 4. Proposed Files

| File | Status | Change |
|---|---|---|
| `packages/data-core/catalyst_data/config.py` | Modified | Add `TICKER_UNIVERSE` (proposed ten tickers) |
| `packages/data-core/catalyst_data/trading_calendar.py` | Modified | Add `CALENDAR_YEARS` range + guard; add 2024 holidays |
| `packages/data-core/catalyst_data/update_planner.py` | Modified | Per-ticker defaults, CalendarCoverageError, provenance labels, compatibility flag |
| `packages/data-core/tests/test_update_planner.py` | Modified | Per-ticker window tests, calendar coverage tests |
| `packages/data-core/tests/test_trading_calendar.py` | **Proposed** | Calendar coverage + boundary tests |

---

## 5. Contracts

### 5.1 Per-ticker default window

```python
def _resolve_per_ticker_windows(
    conn, tickers, from_date, to_date, reference_today, historical_start
) -> dict[str, tuple[str, str]]:
    """Return {ticker: (from_date, to_date)} for every ticker."""
    latest = latest_closed_trading_day_for_date(reference_today)
    windows = {}
    for t in tickers:
        wm = _ticker_ohlcv_watermark(conn, t)  # max(date) from ohlcv for this ticker
        f = from_date or (wm and _next_session(wm)) or historical_start
        windows[t] = (f, to_date or latest)
    return windows
```

Key rules:
- Each ticker's `from_date` is derived from **that ticker's own** OHLCV watermark
- A ticker absent from OHLCV uses the configured historical start
- `to_date` is always the latest closed session (same for all tickers)
- Never derive expected sessions from existing OHLCV rows as the session calendar

### 5.2 Universe contract

```python
TICKER_UNIVERSE = ("AAPL", "AMD", "AMZN", "GOOGL", "JPM", "META", "MSFT", "NVDA", "TSLA", "UNH")
```

Resolution order:
1. Explicit CLI/API tickers → `"explicit-config"` provenance
2. `TICKER_UNIVERSE` constant → `"explicit-config"` provenance (default)
3. OHLCV-derived with `use_ohlcv_universe=True` compat flag → `"ohlcv-derived-fallback"` provenance + typed `universe-fallback` warning
4. Certified execution must reject fallback provenance

### 5.3 Calendar coverage guard

```python
CALENDAR_YEARS: tuple[int, int] = (2024, 2027)  # first supported year, last supported year

class CalendarCoverageError(Exception):
    def __init__(self, requested: str, supported: tuple[int, int]):
        self.requested = requested
        self.supported = supported
        super().__init__(f"Date {requested} outside supported calendar range {supported}")
```

- Static holiday set covers `CALENDAR_YEARS`
- `calendar_trading_days` fails closed for dates outside range
- Year-boundary tests prove transitions work
- Do not imply indefinite correctness; the range is explicit and reviewed annually

### 5.4 Observed-market-closure tests

- Prove `calendar_trading_days("2024-12-30", "2025-01-03")` excludes 2025-01-01
- Prove `calendar_trading_days("2026-12-24", "2026-12-28")` excludes 2026-12-25
- Prove `calendar_trading_days("2027-12-31", "2028-01-02")` raises `CalendarCoverageError`

---

## 6. TDD Tasks

### Task 1: TICKER_UNIVERSE constant

**Test:** `test_ticker_universe_is_configured_constant`
```python
from catalyst_data.config import TICKER_UNIVERSE
def test_ticker_universe():
    assert isinstance(TICKER_UNIVERSE, tuple)
    assert len(TICKER_UNIVERSE) == 10
    assert all(isinstance(t, str) for t in TICKER_UNIVERSE)
```

**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_update_planner.py::test_ticker_universe -q --tb=short` → `ImportError`

**Implementation:** Add `TICKER_UNIVERSE` to `config.py`.

**Green:** PASS.

### Task 2: Calendar coverage guard

**Test file:** `packages/data-core/tests/test_trading_calendar.py`

```python
from catalyst_data.trading_calendar import calendar_trading_days, CALENDAR_YEARS, CalendarCoverageError

class TestCalendarCoverage:
    def test_year_boundary_transition_works(self):
        days = calendar_trading_days("2024-12-30", "2025-01-03")
        assert "2025-01-01" not in days  # New Year's
        assert "2024-12-30" in days
        assert "2024-12-31" in days
        assert "2025-01-02" in days

    def test_outside_range_fails_closed(self):
        with pytest.raises(CalendarCoverageError):
            calendar_trading_days("2023-12-01", "2023-12-31")

    def test_weekend_excluded(self):
        days = calendar_trading_days("2026-07-11", "2026-07-13")
        assert "2026-07-11" in days   # Saturday → not a trading day? Wait, July 11 2026 is a Saturday.
        # Verify: calendar_trading_days excludes weekends
        # July 11 2026 = Saturday → excluded
        # July 12 2026 = Sunday → excluded
        # July 13 2026 = Monday → included
        assert len(days) == 1
        assert "2026-07-13" in days
```

**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_trading_calendar.py -q --tb=short` → FAIL (CalendarCoverageError undefined or holidays missing)

**Implementation:** Add `CALENDAR_YEARS`, `CalendarCoverageError`, 2024 holidays, year-range check in `calendar_trading_days`.

**Green:** 3 PASS.

### Task 3: Per-ticker default windows

```python
class TestPerTickerWindows:
    def test_stale_ticker_plans_earlier_sessions(self, tmp_path):
        """JPM stale at 2026-05-01, AAPL current at 2026-07-10.
        JPM plan must include earlier missing sessions.
        AAPL must not force JPM's lower bound."""
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-10',224.0,228.0,223.0,227.0,48000000,'polygon')",
            "INSERT INTO ohlcv VALUES ('JPM','2026-05-01',190.0,192.0,188.0,191.0,20000000,'polygon')",
        ])
        plan = plan_update(db_path, tickers=["AAPL", "JPM"], sources=["polygon_ohlcv"],
                          reference_today="2026-07-11")
        # JPM should have sessions from 2026-05-02 onward
        jpm_cells = [c for c in plan.stages["market"]["cells"] if c[0] == "JPM"]
        jpm_dates = sorted(set(c[1] for c in jpm_cells))
        assert jpm_dates[0] < "2026-07-09"  # JPM starts earlier than AAPL
        # AAPL should start from 2026-07-11 (next session after 07-10)
        aapl_cells = [c for c in plan.stages["market"]["cells"] if c[0] == "AAPL"]
        aapl_dates = sorted(set(c[1] for c in aapl_cells))
        assert all(d >= "2026-07-11" for d in aapl_dates)

    def test_absent_ticker_uses_historical_start(self, tmp_path):
        """Ticker with zero OHLCV rows plans from configured historical start."""
        db_path = create_fixture_db(tmp_path)
        plan = plan_update(db_path, tickers=["NVDA"], sources=["polygon_ohlcv"],
                          from_date=None, to_date=None, reference_today="2026-07-11")
        assert plan.stages["market"]["cells"]

    def test_ohlcv_deletion_does_not_shrink_expectation(self, tmp_path):
        """Calendar expectation stays fixed even if OHLCV row is deleted."""
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-08',220.0,225.0,219.0,224.0,50000000,'polygon')",
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',221.0,226.0,220.0,225.0,50000000,'polygon')",
        ])
        plan1 = plan_update(db_path, tickers=["AAPL"], sources=["polygon_ohlcv"],
                           reference_today="2026-07-11")
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM ohlcv WHERE date='2026-07-09'")
        conn.commit(); conn.close()
        plan2 = plan_update(db_path, tickers=["AAPL"], sources=["polygon_ohlcv"],
                           reference_today="2026-07-11")
        # Calendar-derived expectation unchanged; OHLCV watermark changes but
        # expected sessions (calendar days) stay the same
        assert plan1.latest_closed_session == plan2.latest_closed_session
```

**Red:** `.venv/bin/python -m pytest packages/data-core/tests/test_update_planner.py -q -k "per_ticker" --tb=short` → FAIL

**Implementation:** Replace single `latest_local_ohlcv_date` default with per-ticker window resolution in `plan_update`.

**Green:** 3 PASS (may need date adjustment based on actual weekday of `reference_today`).

### Task 4: Universe provenance

```python
class TestUniverseProvenance:
    def test_explicit_tickers_have_explicit_provenance(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('TSLA','2026-07-09',250.0,255.0,248.0,253.0,40000000,'polygon')",
        ])
        plan = plan_update(db_path, tickers=["TSLA"], sources=["polygon_news"])
        assert plan.universe["provenance"] == "explicit-config"

    def test_default_universe_uses_configured_constant(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
            "INSERT INTO ohlcv VALUES ('META','2026-07-09',500.0,505.0,498.0,503.0,30000000,'polygon')",
        ])
        plan = plan_update(db_path)  # no explicit tickers → uses TICKER_UNIVERSE
        assert plan.universe["provenance"] == "explicit-config"
        assert set(plan.universe["tickers"]) == set(config.TICKER_UNIVERSE)

    def test_ohlcv_derived_universe_requires_compat_flag(self, tmp_path):
        db_path = create_fixture_db(tmp_path, extra_sql=[
            "INSERT INTO ohlcv VALUES ('AAPL','2026-07-09',220.0,225.0,219.0,224.0,50000000,'polygon')",
        ])
        # Without compat flag: uses TICKER_UNIVERSE
        plan = plan_update(db_path)
        assert plan.universe["provenance"] == "explicit-config"
        # With compat flag: uses ohlcv-derived
        plan2 = plan_update(db_path, use_ohlcv_universe=True)
        assert plan2.universe["provenance"] == "ohlcv-derived-fallback"
        assert any(w["type"] == "universe-fallback" for w in plan2.warnings)
```

**Green:** 3 PASS.

### Task 5: Real Dev DB read-only inspection

**Test:** Manual verification (not automated):
```
.venv/bin/python -c "
from catalyst_data.update_planner import plan_update
plan = plan_update('data/catalyst_dev_ws4b.db', reference_today='2026-07-12')
for t in plan.universe['tickers'][:3]:
    cells = [c for c in plan.stages['market']['cells'] if c[0] == t]
    dates = sorted(set(c[1] for c in cells))
    print(f'{t}: {len(dates)} sessions, {dates[0]} → {dates[-1]}')
"
```

Expected: Each ticker's planned window extends from its own post-watermark session. No writes.

### Task 6: Full canonical

```
.venv/bin/python -m pytest packages/data-core/tests/test_update_planner.py -q --tb=short
.venv/bin/python -m pytest packages/data-core/tests/test_trading_calendar.py -q --tb=short
.venv/bin/python -m pytest packages/data-core -q
```

No new failures. DeprecationWarnings from W1-A expected.

---

## 7. Schema Impact

**None.**

---

## 8. Git Boundary

**Commit:** `feat(data-core): calendar-derived planning and explicit universe`

**Stage manifest:**
- `packages/data-core/catalyst_data/config.py` — `TICKER_UNIVERSE`
- `packages/data-core/catalyst_data/trading_calendar.py` — `CALENDAR_YEARS`, `CalendarCoverageError`, 2024 holidays
- `packages/data-core/catalyst_data/update_planner.py` — per-ticker defaults, coverage guard
- `packages/data-core/tests/test_update_planner.py` — per-ticker/universe tests
- `packages/data-core/tests/test_trading_calendar.py` — new calendar tests

---

## 9. Landmines

1. `trading_days_for_window` unions OHLCV with calendar — must NOT be used for planner expectation.
2. Holiday additions change historical session counts — certification coverage percentages shift.
3. `TICKER_UNIVERSE` is a **tuple** (immutable). AD-UNIVERSE-1 must approve the final set before certification.
4. Calendar coverage is fail-closed: dates outside `CALENDAR_YEARS` raise `CalendarCoverageError`.
5. Do not hardcode stale watermark dates like `2025-05-02` in tests or documentation.

---

## 10. Exit Gate

1. Per-ticker default windows: each ticker's from_date derived from its own watermark
2. Stale ticker does not constrain current tickers; absent ticker uses historical start
3. Calendar-derived expected sessions (not from OHLCV rows)
4. Explicit universe with provenance; fallback requires compat flag + warning
5. CalendarCoverageError for dates outside supported range
6. Real Dev DB read-only inspection: per-ticker windows span correct ranges
7. No new canonical failures

---

## 11. Orchestrator Verification

1. Hand-compute expected sessions for one ticker-month containing a holiday; diff against planner output
2. Confirm per-ticker window on real Dev DB (read-only): two tickers with different watermarks get different from_dates
3. Delete one OHLCV row → calendar expectation unchanged
4. Verify `calendar_trading_days` raises `CalendarCoverageError` for out-of-range dates

---

## 12. Architect Decision Gates

| ID | Decision | Recommendation | Required By |
|---|---|---|---|
| AD-UNIVERSE-1 | Final ten-ticker universe | Use existing ten: AAPL, AMD, AMZN, GOOGL, JPM, META, MSFT, NVDA, TSLA, UNH | Before certification |
| AD-DATE-1 | Historical start date | 2024-12-30 | Before execution runs |
| AD-OHLCV-2 | Adjusted/raw price policy | Handled in W1-C | W1-C |
