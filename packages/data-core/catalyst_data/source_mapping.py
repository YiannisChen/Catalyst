from __future__ import annotations

def map_logical_source(source: str) -> list[str]:
    """
    Map a logical source name (what the caller requests) to the concrete
    provider endpoints we ingest in data-core.

    Note: we intentionally return `list[str]` here to avoid depending on a
    `data_core.types` module that may not exist in this repo snapshot.
    """
    if source == "fmp_fundamentals":
        return ["income_statement", "balance_sheet", "cash_flow"]
    if source == "yfinance_fundamentals":
        return ["income_statement", "balance_sheet", "cash_flow"]
    if source == "polygon_news":
        return ["news"]
    if source == "polygon_ohlcv":
        return ["ohlcv"]
    if source == "fred_macro":
        # Common FRED series used in the system design spec.
        return ["DFF", "DGS10", "VIXCLS", "UNRATE", "CPIAUCSL"]
    if source == "sec_filings":
        return ["sec_submissions"]
    return [source]
