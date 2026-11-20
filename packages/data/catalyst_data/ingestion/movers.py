"""Same-day movers: stocks that moved with the target on D beyond what market and peers explain.

Model per stock (E2/E3, vectorised): r = a + bM·SPY + bG·(G − SPY) + e over the 250 sessions before D, G = mean of
the stock's five most correlated same-sector stocks (GICS sector from the peer map), shrinkage n / (n + 60) of bM
toward 1 and bG toward 0; commodity producers add their channel ETF (E2/E3 CHANNEL: GLD, CPER, USO), shrunk toward
0. Residual z on D = residual / residual SD of the fit. Returns are dividend-adjusted (``corp_actions``). The
universe is the peer map's GICS tickers with bars in the market store.
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import numpy as np

from catalyst_data.ingestion import corp_actions
from catalyst_data.storage.connect import connect_readonly

WINDOW, PEERS, PRIOR_K, MIN_OBS = 250, 5, 60, 150
CHANNEL = {"NEM": "GLD", "FCX": "CPER", **{t: "USO" for t in ("XOM", "CVX", "COP", "EOG", "OXY", "SLB")}}


def _matrix(market_db: Path, symbols: list[str], day: str) -> tuple[list[str], np.ndarray, np.ndarray]:
    """(dates, close-to-close returns %, gap returns %) for the WINDOW + 1 sessions ending at ``day``."""
    with connect_readonly(market_db, immutable=False, timeout_ms=10_000) as conn:
        dates = [d for (d,) in conn.execute(
            "SELECT DISTINCT date FROM ohlcv WHERE symbol='SPY' AND date<=? ORDER BY date DESC LIMIT ?",
            (day, WINDOW + 2))][::-1]
        if not dates or dates[-1] != day:
            raise ValueError(f"no SPY bar for {day}")
        index = {d: i for i, d in enumerate(dates)}
        col = {s: j for j, s in enumerate(symbols)}
        close = np.full((len(dates), len(symbols)), np.nan)
        opens = np.full((len(dates), len(symbols)), np.nan)
        marks = ",".join("?" * len(symbols))
        for symbol, d, o, c in conn.execute(
                f"SELECT symbol, date, open, close FROM ohlcv WHERE date>=? AND date<=? AND symbol IN ({marks})",
                (dates[0], day, *symbols)):
            if d in index and c:
                close[index[d], col[symbol]] = c
                opens[index[d], col[symbol]] = o if o else np.nan
    div = np.zeros_like(close)
    for j, symbol in enumerate(symbols):
        for i, d in enumerate(dates):
            cash = corp_actions.dividend(symbol, d)
            if cash:
                div[i, j] = cash
    ret = ((close[1:] + div[1:]) / close[:-1] - 1.0) * 100.0
    gap = ((opens[1:] + div[1:]) / close[:-1] - 1.0) * 100.0
    return dates[1:], ret, gap


def _panel(market_db: Path, peer_map: str | Path, day: str) -> dict:
    gics = json.loads(Path(peer_map).read_text())["gics"]["tickers"]
    channels = sorted(set(CHANNEL.values()))
    stocks = sorted(t for t in gics if t not in ("SPY", *channels))
    symbols = ["SPY"] + stocks + channels
    dates, ret, gap = _matrix(Path(market_db), symbols, day)
    filled_hist = slice(0, len(dates) - 1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # symbols with no history in the window
        filled = np.where(np.isnan(ret[filled_hist]), np.nan_to_num(np.nanmean(ret[filled_hist], axis=0)),
                          ret[filled_hist])
    with np.errstate(invalid="ignore", divide="ignore"):
        corr = np.corrcoef(filled.T)
    return {"symbols": symbols, "dates": dates, "ret": ret, "gap": gap, "corr": corr, "n_stocks": 1 + len(stocks),
            "sector": [(gics.get(s) or [None])[0] for s in symbols],
            "channel_col": {c: symbols.index(c) for c in channels}}


def _resid_z(p: dict, j: int) -> float | None:
    """Residual z on D of column ``j`` under the market + correlated-peers (+ channel) model; None when unfit."""
    symbols, dates, ret, corr, sector = p["symbols"], p["dates"], p["ret"], p["corr"], p["sector"]
    spy = ret[:, 0]
    hist, today = slice(0, len(dates) - 1), len(dates) - 1
    if np.isnan(ret[today, j]) or np.isnan(spy[today]):
        return None
    same = [k for k in range(1, p["n_stocks"]) if k != j and sector[k] == sector[j] and sector[j]
            and np.sum(~np.isnan(ret[hist, k])) >= MIN_OBS]
    if len(same) < 2:
        return None
    peers = sorted(same, key=lambda k: -np.nan_to_num(corr[j, k], nan=-1))[:PEERS]
    g = np.nanmean(ret[:, peers], axis=1)
    columns = [np.ones(len(dates)), spy, g - spy]
    if symbols[j] in CHANNEL:
        columns.append(ret[:, p["channel_col"][CHANNEL[symbols[j]]]])
    X = np.column_stack(columns)
    ok = ~np.isnan(ret[hist, j]) & ~np.isnan(X[hist]).any(axis=1)
    if ok.sum() < MIN_OBS or np.isnan(X[today]).any():
        return None
    coef, *_ = np.linalg.lstsq(X[hist][ok], ret[hist, j][ok], rcond=None)
    weight = ok.sum() / (ok.sum() + PRIOR_K)
    coef[1] = weight * coef[1] + (1 - weight) * 1.0
    coef[2:] = weight * coef[2:]
    sd = float(np.std(ret[hist, j][ok] - X[hist][ok] @ coef)) or 1.0
    return float((ret[today, j] - X[today] @ coef) / sd)


def resid_z(market_db: str | Path, peer_map: str | Path, ticker: str, day: str) -> float | None:
    """The same-day-movers residual z of one stock on ``day`` (None when the model cannot be fit)."""
    p = _panel(Path(market_db), peer_map, day)
    if ticker.upper() not in p["symbols"][1:p["n_stocks"]]:
        return None
    return _resid_z(p, p["symbols"].index(ticker.upper()))


def same_day_movers(market_db: str | Path, peer_map: str | Path, ticker: str, day: str, direction: int,
                    *, threshold: float = 2.0, limit: int = 20) -> dict:
    """Stocks moving in ``direction`` (+1/−1) on ``day`` with residual |z| > threshold, strongest first."""
    p = _panel(Path(market_db), peer_map, day)
    symbols, ret, gap, sector = p["symbols"], p["ret"], p["gap"], p["sector"]
    today = len(p["dates"]) - 1
    movers, by_sector = [], {}
    for j in range(1, p["n_stocks"]):
        z = _resid_z(p, j)
        if z is None:
            continue
        if abs(z) <= threshold or np.sign(z) != np.sign(direction) or np.sign(ret[today, j]) != np.sign(direction):
            continue
        g_today = gap[today, j]
        movers.append({"ticker": symbols[j], "sector": sector[j], "return_pct": round(float(ret[today, j]), 2),
                       "resid_z": round(z, 2), "gap_pct": None if np.isnan(g_today) else round(float(g_today), 2),
                       "is_target": symbols[j] == ticker.upper()})
        by_sector[sector[j]] = by_sector.get(sector[j], 0) + 1
    movers.sort(key=lambda m: -abs(m["resid_z"]))
    return {"session_date": day, "direction": "up" if direction > 0 else "down", "threshold": threshold,
            "count": len(movers), "by_sector": dict(sorted(by_sector.items(), key=lambda kv: -kv[1])),
            "movers": movers[:limit]}


__all__ = ["resid_z", "same_day_movers"]
