"""Tests for the data-core M9 scope helpers."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest
from catalyst_data.ingestion.scope import (
    commodity_aligned,
    market_aligned,
    resolve_context,
)


@pytest.mark.parametrize("predicate", [commodity_aligned, market_aligned])
def test_aligned_requires_same_sign_and_at_least_one_percent(predicate) -> None:
    if predicate is commodity_aligned:
        aligned = lambda target, move: predicate(target, "GLD", move)
    else:
        aligned = predicate
    assert aligned(2.0, 1.0) is True
    assert aligned(-2.0, -1.0) is True
    assert aligned(2.0, -1.0) is False
    assert aligned(-2.0, 1.0) is False
    assert aligned(2.0, 0.99) is False
    assert aligned(-2.0, -0.99) is False
    assert aligned(None, 2.0) is False
    assert aligned(2.0, None) is False
    if predicate is commodity_aligned:
        assert predicate(2.0, None, 2.0) is False


def _create_market(path, target_db) -> None:
    market = sqlite3.connect(path)
    market.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, close REAL)")
    market.execute("CREATE TABLE issuer_sic (ticker TEXT, sic TEXT)")
    market.executemany(
        "INSERT INTO issuer_sic VALUES (?, ?)",
        [("TARGET", "3571"), ("GOOD", "3571"), ("WEAK", "3571")],
    )
    start = date(2026, 1, 1)
    target_rows = []
    peer_rows = []
    target_close = good_close = weak_close = 100.0
    for i in range(61):
        day = (start + timedelta(days=i)).isoformat()
        target_rows.append(("TARGET", day, target_close))
        peer_rows.extend([("GOOD", day, good_close), ("WEAK", day, weak_close)])
        move = 0.01 if i % 2 else -0.008
        target_close *= 1 + move
        good_close *= 1 + move
        weak_close *= 1 - move
    target_db.executemany("INSERT INTO ohlcv VALUES (?, ?, ?)", target_rows)
    market.executemany("INSERT INTO ohlcv VALUES (?, ?, ?)", peer_rows)
    market.commit()
    market.close()


def test_resolve_context_uses_mapped_direct_peers(tmp_path) -> None:
    snapshot = sqlite3.connect(":memory:")
    snapshot.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, close REAL)")
    peer_map = {
        "benchmark": "SPY",
        "targets": {"TARGET": {"sector_etf": "XLK", "direct_peers": ["A", "B"]}},
    }
    assert resolve_context(snapshot, None, peer_map, "TARGET", "2026-04-01") == (
        "SPY", "XLK", ("A", "B")
    )


def test_resolve_context_ranks_same_sic_peers_and_excludes_low_correlation(tmp_path) -> None:
    market_path = tmp_path / "market.sqlite"
    snapshot = sqlite3.connect(":memory:")
    snapshot.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, close REAL)")
    _create_market(market_path, snapshot)
    peer_map = {
        "benchmark": "SPY",
        "sic_sector_fallback": [{"sic_from": 3500, "sic_to": 3599, "etf": "XLK"}],
    }

    assert resolve_context(snapshot, market_path, peer_map, "TARGET", "2026-04-01") == (
        "SPY", "XLK", ("GOOD",)
    )


def test_resolve_context_prefers_correlated_gics_subindustry_peers(tmp_path) -> None:
    market_path = tmp_path / "market.sqlite"
    snapshot = sqlite3.connect(":memory:")
    snapshot.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, close REAL)")
    _create_market(market_path, snapshot)
    peer_map = {
        "benchmark": "SPY",
        "sic_sector_fallback": [{"sic_from": 3500, "sic_to": 3599, "etf": "SIC_ETF"}],
        "gics": {"tickers": {
            "TARGET": ["Information Technology", "Semiconductors"],
            "GOOD": ["Information Technology", "Semiconductors"],
            "WEAK": ["Information Technology", "Semiconductors"],
        }},
    }

    assert resolve_context(snapshot, market_path, peer_map, "TARGET", "2026-04-01") == (
        "SPY", "XLK", ("GOOD",)
    )


def test_resolve_context_falls_back_to_sic_when_gics_pool_has_no_correlated_peer(tmp_path) -> None:
    market_path = tmp_path / "market.sqlite"
    snapshot = sqlite3.connect(":memory:")
    snapshot.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, close REAL)")
    _create_market(market_path, snapshot)
    peer_map = {
        "benchmark": "SPY",
        "sic_sector_fallback": [{"sic_from": 3500, "sic_to": 3599, "etf": "SIC_ETF"}],
        "gics": {"tickers": {
            "TARGET": ["Information Technology", "Chips"],
            "WEAK": ["Information Technology", "Chips"],
        }},
    }

    assert resolve_context(snapshot, market_path, peer_map, "TARGET", "2026-04-01") == (
        "SPY", "XLK", ("GOOD",)
    )
