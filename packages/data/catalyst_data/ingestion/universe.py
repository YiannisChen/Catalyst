"""S&P 500 current constituents and GICS classification helpers."""
from __future__ import annotations

import json
from json import JSONDecoder

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

GICS_SECTOR_ETF = {
    "Information Technology": "XLK",
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
}


def normalize_ticker(symbol: str) -> str:
    """Return the SEC/yfinance ticker form used by the M9 stores."""
    return symbol.strip().replace(".", "-")


def parse_sp500(html: str) -> list[dict[str, str]]:
    """Extract the current S&P 500 constituents from Wikipedia's table."""
    from bs4 import BeautifulSoup  # ingestion-only dependency; keep scope import light

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="constituents")
    if table is None:
        raise ValueError("S&P 500 constituents table is missing")

    header = table.find("tr")
    headers = [cell.get_text(" ", strip=True) for cell in header.find_all(["th", "td"])] if header else []
    positions = {name: index for index, name in enumerate(headers)}
    required = ("Symbol", "Security", "GICS Sector", "GICS Sub-Industry", "CIK")
    if any(name not in positions for name in required):
        raise ValueError("S&P 500 constituents table is missing required columns")

    rows: list[dict[str, str]] = []
    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if not cells or cells[0].name == "th":
            continue
        values = [cell.get_text(" ", strip=True) for cell in cells]
        if len(values) < len(headers):
            continue
        rows.append({
            "ticker": normalize_ticker(values[positions["Symbol"]]),
            "name": values[positions["Security"]],
            "gics_sector": values[positions["GICS Sector"]],
            "gics_sub_industry": values[positions["GICS Sub-Industry"]],
            "cik": values[positions["CIK"]],
        })
    if len(rows) < 400:
        raise ValueError(f"S&P 500 constituents table has fewer than 400 rows ({len(rows)})")
    return rows


def gics_block(rows: list[dict[str, str]], source_url: str, fetched_at: str) -> dict:
    return {
        "source": source_url,
        "fetched_at": fetched_at,
        "note": "current constituents (survivorship: membership is as of fetched_at, not point-in-time)",
        "tickers": {
            row["ticker"]: [row["gics_sector"], row["gics_sub_industry"]]
            for row in rows
        },
    }


def _top_level_value_span(text: str, wanted: str) -> tuple[int, int] | None:
    decoder = JSONDecoder()
    cursor = text.find("{") + 1
    while cursor < len(text):
        while cursor < len(text) and (text[cursor].isspace() or text[cursor] == ","):
            cursor += 1
        if cursor >= len(text) or text[cursor] == "}":
            return None
        key_start = cursor
        key, cursor = decoder.raw_decode(text, cursor)
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text) or text[cursor] != ":":
            raise ValueError("invalid JSON object")
        cursor += 1
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        value_start = cursor
        _, value_end = decoder.raw_decode(text, value_start)
        if key == wanted:
            return value_start, value_end
        cursor = value_end
    return None


def splice_gics_block(text: str, block: dict) -> str:
    """Replace or add the top-level gics value while retaining other text."""
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("peer map must be a JSON object")
    encoded = json.dumps(block, indent=1, ensure_ascii=False)
    span = _top_level_value_span(text, "gics")
    if span is not None:
        start, end = span
        return text[:start] + encoded + text[end:]

    opening = text.find("{")
    if opening < 0:
        raise ValueError("peer map must be a JSON object")
    addition = '\n "gics": ' + encoded
    if parsed:
        addition += ","
    return text[:opening + 1] + addition + text[opening + 1:]
