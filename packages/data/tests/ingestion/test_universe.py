"""Tests for the S&P 500 GICS universe helpers."""
from __future__ import annotations

import json

import pytest
from catalyst_data.ingestion.universe import (
    gics_block,
    normalize_ticker,
    parse_sp500,
    splice_gics_block,
)


def _sp500_html(row_count: int = 400) -> str:
    rows = []
    for index in range(row_count):
        symbol = "BRK.B" if index == 0 else f"T{index:03}"
        rows.append(
            "<tr>"
            f"<td>{symbol}</td><td>Company {index}</td>"
            "<td>Information Technology</td><td>Semiconductors</td>"
            "<td>Somewhere</td><td>2020-01-01</td>"
            f"<td>{index + 1:010d}</td><td>2000</td>"
            "</tr>"
        )
    return (
        '<table id="constituents"><thead><tr>'
        "<th>Symbol</th><th>Security</th><th>GICS Sector</th>"
        "<th>GICS Sub-Industry</th><th>Headquarters</th><th>Date added</th>"
        "<th>CIK</th><th>Founded</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def test_parse_sp500_extracts_normalized_constituents() -> None:
    rows = parse_sp500(_sp500_html())

    assert len(rows) == 400
    assert rows[0] == {
        "ticker": "BRK-B",
        "name": "Company 0",
        "gics_sector": "Information Technology",
        "gics_sub_industry": "Semiconductors",
        "cik": "0000000001",
    }


def test_parse_sp500_rejects_missing_constituents_table() -> None:
    with pytest.raises(ValueError, match="constituents"):
        parse_sp500("<html><table id='other'></table></html>")


def test_parse_sp500_rejects_too_few_rows() -> None:
    with pytest.raises(ValueError, match="400"):
        parse_sp500(_sp500_html(399))


def test_normalize_ticker_uses_yfinance_sec_separator() -> None:
    assert normalize_ticker(" BRK.B ") == "BRK-B"


def test_gics_block_shape() -> None:
    assert gics_block(
        [{
            "ticker": "BRK-B",
            "name": "Berkshire Hathaway",
            "gics_sector": "Financials",
            "gics_sub_industry": "Multi-Sector Holdings",
            "cik": "0001067983",
        }],
        "https://example.test/sp500",
        "2026-10-03T12:00:00Z",
    ) == {
        "source": "https://example.test/sp500",
        "fetched_at": "2026-10-03T12:00:00Z",
        "note": "current constituents (survivorship: membership is as of fetched_at, not point-in-time)",
        "tickers": {"BRK-B": ["Financials", "Multi-Sector Holdings"]},
    }


def test_splice_gics_block_keeps_existing_hand_formatted_text_unchanged() -> None:
    original = '{\n  "benchmark": "SPY",\n  "targets": {}\n}\n'
    block = {"source": "fixture", "tickers": {"ABC": ["Sector", "Industry"]}}

    result = splice_gics_block(original, block)

    assert json.loads(result)["gics"] == block
    assert result.startswith('{\n "gics": {')
    assert result.endswith(original[1:])
