"""FRED curated series manifest for macro_observations.

Defines the 12-series set with attribution category grouping, release cadence,
and per-series freshness thresholds. TEDRATE is removed (stale, ~2022).
T10Y2Y is derived (computed from DGS10 − DGS2, not fetched from FRED).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet


@dataclass(frozen=True)
class FredSeries:
    series_id: str
    name: str
    category: str       # macro_rates | macro_inflation | macro_labor_growth | market_risk_liquidity
    cadence: str        # daily | monthly | quarterly
    stale_days: int     # max days behind before STALE
    derived: bool = False  # True if computed from other series, not fetched


# — curated 12-series manifest (TEDRATE removed, T10Y2Y added) —
CURATED_SERIES: FrozenSet[FredSeries] = frozenset({
    # macro_rates — daily
    FredSeries("DFF", "Federal Funds Rate", "macro_rates", "daily", 2),
    FredSeries("DGS10", "10-Year Treasury", "macro_rates", "daily", 2),
    FredSeries("DGS2", "2-Year Treasury", "macro_rates", "daily", 2),
    FredSeries("DAAA", "Moody's Aaa Yield", "macro_rates", "daily", 2),
    FredSeries("DBAA", "Moody's Baa Yield", "macro_rates", "daily", 2),

    # macro_inflation — monthly
    FredSeries("CPIAUCSL", "CPI All-Urban", "macro_inflation", "monthly", 35),
    FredSeries("PCEPI", "PCE Price Index", "macro_inflation", "monthly", 35),

    # macro_labor_growth — monthly/quarterly
    FredSeries("UNRATE", "Unemployment Rate", "macro_labor_growth", "monthly", 35),
    FredSeries("PAYEMS", "Nonfarm Payrolls", "macro_labor_growth", "monthly", 35),
    FredSeries("GDP", "Gross Domestic Product", "macro_labor_growth", "quarterly", 100),

    # market_risk_liquidity — daily
    FredSeries("VIXCLS", "VIX Close", "market_risk_liquidity", "daily", 2),

    # derived (not fetched — computed from DGS10 − DGS2)
    FredSeries("T10Y2Y", "10Y-2Y Treasury Spread", "macro_rates", "daily", 2, derived=True),
})


# source_mapping list (all series EXCEPT derived T10Y2Y)
FETCHED_SERIES: list[str] = sorted(
    [s.series_id for s in CURATED_SERIES if not s.derived]
)


# quick lookups
def series_by_id(series_id: str) -> FredSeries | None:
    for s in CURATED_SERIES:
        if s.series_id == series_id:
            return s
    return None


def series_by_category(category: str) -> list[FredSeries]:
    return [s for s in CURATED_SERIES if s.category == category and not s.derived]
