"""Group-level market checks for the inferred-link channels (experiment B, design 2026-10-19-b §2–§3, locked).

Price facts only, no model calls. Returns are dividend-adjusted (``movers._matrix``) over the WINDOW sessions
before D.
- Market-model residual z: r = a + b·SPY + e over the window, b shrunk n / (n + 60) toward 1; z = e_D / sd(e).
- Raw z: (r_D − mean) / sd over the window (commodity ETFs).
- A group is ``ETF:<symbol>`` or ``SUB:<GICS sub-industry>``. Members: a sector ETF -> its GICS sector's stocks;
  SMH -> the Semiconductors and Semiconductor Materials & Equipment sub-industries; a commodity ETF -> the stocks
  of the producer sub-industries the frozen exposure map lists; SUB -> that sub-industry's stocks.
- Group z: commodity ETF raw z; sector ETF and SMH market-model z; SUB the market-model z of the equal-weight
  basket of its members other than the target.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

import numpy as np

from catalyst_data.ingestion.movers import _matrix

PRIOR_K, MIN_OBS = 60, 150
COMMODITY_ETFS = ("USO", "UNG", "GLD", "SLV", "CPER")
SECTOR_ETFS = {"XLB": "Materials", "XLC": "Communication Services", "XLE": "Energy", "XLF": "Financials",
               "XLI": "Industrials", "XLK": "Information Technology", "XLP": "Consumer Staples",
               "XLRE": "Real Estate", "XLU": "Utilities", "XLV": "Health Care", "XLY": "Consumer Discretionary"}
SMH_SUBS = ("Semiconductors", "Semiconductor Materials & Equipment")
MIN_MEMBERS = 4


@lru_cache(maxsize=4)
def _gics(peer_map: str) -> dict[str, list[str]]:
    return json.loads(Path(peer_map).read_text())["gics"]["tickers"]


def members(group: str, peer_map: str | Path, producer_subs: tuple[str, ...] = ()) -> list[str]:
    """All universe stocks of ``group``, sorted. ``producer_subs`` ("Sector / Sub-industry") for commodity ETFs."""
    gics = _gics(str(peer_map))
    kind, _, name = group.partition(":")
    if kind == "SUB":
        return sorted(t for t, v in gics.items() if len(v) > 1 and v[1] == name)
    if kind == "ETF" and name in SECTOR_ETFS:
        return sorted(t for t, v in gics.items() if v and v[0] == SECTOR_ETFS[name])
    if kind == "ETF" and name == "SMH":
        return sorted(t for t, v in gics.items() if len(v) > 1 and v[1] in SMH_SUBS)
    if kind == "ETF" and name in COMMODITY_ETFS:
        wanted = {tuple(s.split(" / ", 1)) for s in producer_subs}
        return sorted(t for t, v in gics.items() if len(v) > 1 and (v[0], v[1]) in wanted)
    return []


def _market_z(series: np.ndarray, spy: np.ndarray) -> float | None:
    hist, today = slice(0, len(series) - 1), len(series) - 1
    if np.isnan(series[today]) or np.isnan(spy[today]):
        return None
    ok = ~np.isnan(series[hist]) & ~np.isnan(spy[hist])
    n = int(ok.sum())
    if n < MIN_OBS:
        return None
    X = np.column_stack([np.ones(n), spy[hist][ok]])
    coef, *_ = np.linalg.lstsq(X, series[hist][ok], rcond=None)
    w = n / (n + PRIOR_K)
    coef[1] = w * coef[1] + (1 - w) * 1.0
    resid = series[hist][ok] - X @ coef
    sd = float(np.std(resid)) or 1.0
    return float((series[today] - coef[0] - coef[1] * spy[today]) / sd)


def _raw_z(series: np.ndarray) -> float | None:
    hist = series[:-1][~np.isnan(series[:-1])]
    if np.isnan(series[-1]) or len(hist) < MIN_OBS:
        return None
    sd = float(np.std(hist)) or 1.0
    return float((series[-1] - float(np.mean(hist))) / sd)


class DayPanel:
    """Return matrix for one session over SPY, the ETFs and the universe, computed once and queried per group."""

    def __init__(self, market_db: str | Path, peer_map: str | Path, day: str):
        self.peer_map = str(peer_map)
        stocks = sorted(_gics(self.peer_map))
        self.symbols = ["SPY", *SECTOR_ETFS, "SMH", *COMMODITY_ETFS, *[s for s in stocks if s != "SPY"]]
        self.dates, self.ret, self.gap = _matrix(Path(market_db), self.symbols, day)
        self.col = {s: j for j, s in enumerate(self.symbols)}
        self.spy = self.ret[:, 0]
        self._z: dict[str, float | None] = {}

    def series(self, symbol: str) -> np.ndarray | None:
        j = self.col.get(symbol)
        return None if j is None else self.ret[:, j]

    def market_z(self, symbol: str) -> float | None:
        if symbol not in self._z:
            s = self.series(symbol)
            self._z[symbol] = None if s is None else _market_z(s, self.spy)
        return self._z[symbol]

    def basket(self, symbols: list[str]) -> np.ndarray:
        cols = [self.col[s] for s in symbols if s in self.col]
        with np.errstate(invalid="ignore"):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                return np.nanmean(self.ret[:, cols], axis=1) if cols else np.full(len(self.dates), np.nan)

    def group_series(self, group: str, target: str, producer_subs: tuple[str, ...] = ()) -> np.ndarray | None:
        kind, _, name = group.partition(":")
        if kind == "ETF":
            return self.series(name)
        others = [m for m in members(group, self.peer_map, producer_subs) if m != target.upper()]
        return self.basket(others) if others else None

    def group_z(self, group: str, target: str, producer_subs: tuple[str, ...] = ()) -> float | None:
        s = self.group_series(group, target, producer_subs)
        if s is None:
            return None
        kind, _, name = group.partition(":")
        return _raw_z(s) if (kind == "ETF" and name in COMMODITY_ETFS) else _market_z(s, self.spy)

    def cluster(self, group: str, target: str, direction: int, producer_subs: tuple[str, ...] = (),
                *, frac: float, floor: int = 3, z: float = 2.0) -> dict:
        """Members other than the target with a same-direction market-model residual z beyond ``z``. n counts every
        member other than the target, so missing data never lowers k; a member without a z is not a hit."""
        others = [m for m in members(group, self.peer_map, producer_subs) if m != target.upper()]
        zs = {m: self.market_z(m) if m in self.col else None for m in others}
        n = len(others)
        k = max(floor, math.ceil(frac * n))
        hits = sorted(m for m, v in zs.items() if v is not None and v * direction > z)
        return {"n": n, "n_with_data": sum(v is not None for v in zs.values()), "k": k, "hits": hits,
                "count": len(hits), "usable": n >= MIN_MEMBERS, "pass": n >= MIN_MEMBERS and len(hits) >= k}


__all__ = ["COMMODITY_ETFS", "DayPanel", "MIN_MEMBERS", "SECTOR_ETFS", "members"]
