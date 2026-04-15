from __future__ import annotations

import argparse

import pytest

import scripts.smoke_test as smoke_test
from scripts.smoke_test import resolve_sources


def test_resolve_sources_returns_available_when_not_requested():
    available = ["polygon_news", "polygon_ohlcv", "fmp_fundamentals"]

    assert resolve_sources(None, available) == available


def test_resolve_sources_filters_requested_sources_in_order():
    available = ["polygon_news", "polygon_ohlcv", "fmp_fundamentals"]

    resolved = resolve_sources("fmp_fundamentals,polygon_news", available)

    assert resolved == ["fmp_fundamentals", "polygon_news"]


def test_resolve_sources_rejects_unknown_source():
    available = ["polygon_news", "polygon_ohlcv", "fmp_fundamentals"]

    with pytest.raises(ValueError, match="Unknown logical source"):
        resolve_sources("not_a_source", available)


def test_resolve_sources_rejects_unavailable_source():
    available = ["polygon_news", "polygon_ohlcv"]

    with pytest.raises(ValueError, match="not available"):
        resolve_sources("fmp_fundamentals", available)


@pytest.mark.asyncio
async def test_run_passes_db_path_to_process_request(tmp_path, monkeypatch):
    package_root = tmp_path / "packages" / "data-core"
    package_root.mkdir(parents=True)
    monkeypatch.setattr(smoke_test, "_PACKAGE_ROOT", package_root)
    monkeypatch.setattr(smoke_test, "get_available_sources", lambda: ["polygon_news"])
    monkeypatch.setattr(
        smoke_test, "get_recent_trading_days", lambda days: ["2026-01-15"]
    )

    class DummyClient:
        async def aclose(self) -> None:
            return None

    async def fake_fetch_fn(
        ticker: str, endpoint: str, date: str
    ):  # pragma: no cover - never called here
        raise AssertionError("fetch_fn should not be called by the stubbed orchestrator")

    async def fake_build_fetch_fn():
        return fake_fetch_fn, DummyClient()

    observed: dict[str, object] = {}

    async def fake_process_request(
        *,
        ticker: str,
        date: str,
        sources: list[str],
        db_path: str,
        fetch_fn,
        limiter=None,
        conn=None,
    ) -> list[dict]:
        observed.update(
            {
                "ticker": ticker,
                "date": date,
                "sources": sources,
                "db_path": db_path,
                "fetch_fn": fetch_fn,
                "limiter": limiter,
                "conn": conn,
            }
        )
        return [{"source": "polygon_news", "ok": True, "error": None}]

    monkeypatch.setattr(smoke_test, "_build_fetch_fn", fake_build_fetch_fn)
    monkeypatch.setattr(smoke_test, "process_request", fake_process_request)

    args = argparse.Namespace(
        ticker="AAPL",
        days=1,
        sources=None,
        artifacts=False,
        verbose=False,
    )

    await smoke_test.run(args)

    assert observed["ticker"] == "AAPL"
    assert observed["date"] == "2026-01-15"
    assert observed["sources"] == ["polygon_news"]
    assert observed["db_path"] == package_root.parent.parent / "data" / "dev_assets.db"
    assert observed["fetch_fn"] is fake_fetch_fn
    assert observed["limiter"] is None
    assert observed["conn"] is None
