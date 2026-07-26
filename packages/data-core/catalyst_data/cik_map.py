"""SEC CIK ↔ ticker map for the ratified B2-O 40-ticker universe.

Loads from a pinned CSV asset.  Provides forward and reverse lookups.
Can refresh from the live SEC company_tickers.json when explicitly invoked.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

_ASSET_DIR = Path(__file__).resolve().parent.parent / "data" / "cik_map"
_CSV_PATH = _ASSET_DIR / "cik_ticker_map.csv"

SUPPORTED_TICKERS = frozenset([
    "AAPL", "AMD", "AMZN", "GOOGL", "JPM", "META", "MSFT", "NVDA", "TSLA", "UNH",
    "INTC", "QCOM", "TSM", "MU", "LRCX", "ASML", "DELL", "HPQ", "CRM", "ADBE",
    "NOW", "ORCL", "PINS", "RDDT", "SNAP", "WMT", "TGT", "COST", "DASH", "F",
    "LCID", "GM", "RIVN", "C", "GS", "BAC", "MS", "CNC", "HUM", "CI",
])

_ticker_to_cik: dict[str, str] = {}
_cik_to_ticker: dict[str, str] = {}


def _load():
    """Load the CIK map from CSV on module import."""
    if not _CSV_PATH.exists():
        raise FileNotFoundError(
            f"CIK map not found at {_CSV_PATH}. "
            f"Run: python -m catalyst_data.cli_index refresh-cik-map"
        )

    with open(_CSV_PATH, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            t = row["ticker"].strip().upper()
            c = row["cik"].strip().zfill(10)
            _ticker_to_cik[t] = c
            _cik_to_ticker[c] = t

    missing = SUPPORTED_TICKERS - set(_ticker_to_cik.keys())
    if missing:
        raise RuntimeError(
            f"CIK map missing tickers: {sorted(missing)}. "
            f"Run: python -m catalyst_data.cli_index refresh-cik-map"
        )


def ticker_to_cik(ticker: str) -> str:
    """Return the zero-padded 10-digit CIK for a ticker."""
    t = ticker.strip().upper()
    if t not in _ticker_to_cik:
        raise KeyError(f"Ticker not in CIK map: {t}")
    return _ticker_to_cik[t]


def cik_to_ticker(cik: str) -> str:
    """Return the ticker for a zero-padded 10-digit CIK."""
    c = cik.strip().zfill(10)
    if c not in _cik_to_ticker:
        raise KeyError(f"CIK not in ticker map: {c}")
    return _cik_to_ticker[c]


def refresh_cik_map(output_path: str | None = None) -> dict[str, str]:
    """Fetch SEC company_tickers.json, validate, and write CSV.

    Returns {ticker: cik} for all SUPPORTED_TICKERS.
    Raises RuntimeError if any ticker is missing from the SEC source.
    """
    url = "https://www.sec.gov/files/company_tickers.json"
    ua = os.environ.get("SEC_USER_AGENT", "CatalystResearch/1.0 (catalyst@example.com)")
    req = Request(url, headers={"User-Agent": ua})
    resp = urlopen(req, timeout=15)
    data = json.loads(resp.read().decode())

    # Build lookup: CIK (int) -> ticker -> pad to 10 digits
    sec_map: dict[str, str] = {}
    for entry in data.values():
        t = entry.get("ticker", "").strip().upper()
        c = str(entry.get("cik_str", "")).strip().zfill(10)
        if t and c:
            sec_map[t] = c

    result: dict[str, str] = {}
    missing = []
    for ticker in sorted(SUPPORTED_TICKERS):
        if ticker in sec_map:
            result[ticker] = sec_map[ticker]
        else:
            missing.append(ticker)

    if missing:
        raise RuntimeError(
            f"Tickers not found in SEC company_tickers.json: {missing}"
        )

    path = Path(output_path) if output_path else _CSV_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "cik"])
        for ticker, cik in sorted(result.items()):
            writer.writerow([ticker, cik])

    return result


# Load on import
_load()
