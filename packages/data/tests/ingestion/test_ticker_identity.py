"""Tests for the importable, time-aware ticker-to-issuer identity registry."""
from __future__ import annotations

from datetime import date

import pytest
from catalyst_data.ingestion.ticker_identity import (
    TickerIdentity,
    TickerIdentityRegistry,
    normalize_ticker,
)


def identity(ticker: str, cik: str, name: str, start: str, end: str | None = None) -> TickerIdentity:
    return TickerIdentity(ticker, cik, name, date.fromisoformat(start),
                          date.fromisoformat(end) if end else None)


def test_normalizes_dot_and_hyphen_class_share_notation():
    assert normalize_ticker(" brk.b ") == "BRK-B"
    assert normalize_ticker("BRK-B") == "BRK-B"


def test_resolves_historical_ticker_at_explicit_date():
    registry = TickerIdentityRegistry([
        identity("OLD", "0000000042", "Example Issuer", "2000-01-01", "2010-06-01"),
        identity("NEW", "0000000042", "Example Issuer", "2010-06-01"),
    ])

    assert registry.resolve("OLD", as_of=date(2010, 5, 31)).ticker == "OLD"
    assert registry.resolve("OLD", as_of=date(2010, 6, 1)) is None
    assert registry.resolve("NEW", as_of=date(2010, 6, 1)).cik == "0000000042"


def test_class_share_tickers_share_issuer_but_remain_distinct_symbols():
    registry = TickerIdentityRegistry([
        identity("ABC-A", "0000000042", "Example Issuer", "2000-01-01"),
        identity("ABC-B", "0000000042", "Example Issuer", "2000-01-01"),
    ])

    a = registry.resolve("ABC.A", as_of=date(2020, 1, 1))
    b = registry.resolve("ABC-B", as_of=date(2020, 1, 1))
    assert a.cik == b.cik
    assert a.ticker == "ABC-A"
    assert b.ticker == "ABC-B"
    assert a != b


def test_rejects_overlapping_intervals_for_normalized_ticker():
    first = identity("ABC.A", "0000000042", "Example Issuer", "2000-01-01", "2010-01-01")
    conflicting = identity("ABC-A", "0000000099", "Other Issuer", "2009-12-31")
    with pytest.raises(ValueError, match="overlap"):
        TickerIdentityRegistry([first, conflicting])


def test_rejects_invalid_interval_and_cik():
    with pytest.raises(ValueError, match="valid_to"):
        identity("ABC", "0000000042", "Example Issuer", "2010-01-01", "2010-01-01")
    with pytest.raises(ValueError, match="CIK"):
        identity("ABC", "invalid", "Example Issuer", "2010-01-01")


def test_csv_import_requires_provenance_fields_and_loads_verified_rows(tmp_path):
    path = tmp_path / "ticker_identities.csv"
    path.write_text(
        "ticker,cik,issuer_name,valid_from,valid_to\n"
        "ABC.A,0000000042,Example Issuer,2000-01-01,2010-01-01\n"
        "ABC-B,0000000042,Example Issuer,2010-01-01,\n"
    )

    registry = TickerIdentityRegistry.from_csv(path)
    assert registry.resolve("ABC-A", as_of=date(2005, 1, 1)).issuer_name == "Example Issuer"
    assert registry.resolve("ABC-B", as_of=date(2010, 1, 1)).valid_from == date(2010, 1, 1)

    path.write_text("ticker,cik\nABC,0000000042\n")
    with pytest.raises(ValueError, match="columns"):
        TickerIdentityRegistry.from_csv(path)


def test_resolve_requires_an_explicit_as_of_date():
    registry = TickerIdentityRegistry()
    with pytest.raises(TypeError):
        registry.resolve("ABC")  # type: ignore[call-arg]
