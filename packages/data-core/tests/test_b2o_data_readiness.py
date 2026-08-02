from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from catalyst_data.migrations import MIGRATIONS, run_migrations
from catalyst_data.storage.sqlite import init_db


RATIFIED_TICKERS = [
    "AAPL", "AMD", "AMZN", "GOOGL", "JPM", "META", "MSFT", "NVDA", "TSLA", "UNH",
    "INTC", "QCOM", "TSM", "MU", "LRCX", "ASML", "DELL", "HPQ", "CRM", "ADBE",
    "NOW", "ORCL", "PINS", "RDDT", "SNAP", "WMT", "TGT", "COST", "DASH", "F",
    "LCID", "GM", "RIVN", "C", "GS", "BAC", "MS", "CNC", "HUM", "CI",
]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _spec_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "catalyst_data"
        / "manifests"
        / "universe_v1_2025_08.spec.json"
    )


def _file_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    init_db(conn)
    run_migrations(conn)
    conn.execute("INSERT INTO ingestion_runs (run_id, plan_hash, expected_plan_hash, ticker_list_json, source_list_json, status, allow_stale_ohlcv, allow_stale_ohlcv_overridden, cancel_requested, started_at) VALUES ('legacy-test-run', '', '', '[]', '[]', 'PLANNED', 0, 0, 0, '2026-01-01T00:00:00Z')")
    conn.commit()
    conn.close()
    return db_path


def _load_spec_dict() -> dict:
    return json.loads(_spec_path().read_text())


def _write_spec(path: Path, spec: dict) -> None:
    path.write_text(json.dumps(spec, sort_keys=True, separators=(",", ":")))


def _source_windows(latest: str = "2026-07-23"):
    from catalyst_data.manifests.universe import SourceWindows

    return SourceWindows.for_latest_complete_session(latest)


def test_existing_b2o_entrypoints_match_binding_contract():
    from catalyst_data import update_planner, update_pipeline
    from catalyst_data.index_builder import build_corpus_and_lexical_index

    assert {8, 9, 10}.issubset({m.version for m in MIGRATIONS})
    assert callable(run_migrations)
    assert hasattr(update_planner, "plan_update")

    plan_sig = inspect.signature(update_planner.plan_update)
    assert "source_scopes" in plan_sig.parameters
    assert plan_sig.parameters["source_scopes"].default is None

    execute_sig = inspect.signature(update_pipeline.execute_update)
    for name in ("db", "plan", "transport", "parent_run_id"):
        assert name in execute_sig.parameters
    assert execute_sig.parameters["parent_run_id"].default is None

    combined_sig = inspect.signature(build_corpus_and_lexical_index)
    assert "certified_snapshot_identity" in combined_sig.parameters
    assert "clock" in combined_sig.parameters


def test_b2o_owned_modules_exist_and_do_not_import_eval():
    for module in (
        "catalyst_data.manifests.universe",
        "catalyst_data.manifests.snapshot",
        "catalyst_data.manifests.operations",
        "catalyst_data.b2o",
    ):
        spec = importlib.util.find_spec(module)
        assert spec is not None, module
        text = Path(spec.origin).read_text()
        assert "catalyst_eval" not in text
        assert "packages/eval" not in text
        assert "LexicalIndexManifest" not in text


def test_tracked_universe_spec_contains_exact_ratified_order():
    from catalyst_data.manifests.universe import load_universe_spec

    spec = load_universe_spec(_spec_path())
    assert spec.tickers == RATIFIED_TICKERS
    assert len(set(spec.tickers)) == 40
    assert spec.semantic_hash == spec.compute_semantic_hash()
    for ticker, company in spec.companies.items():
        assert company["legal_name"] != f"{ticker} issuer"
        assert " issuer" not in company["legal_name"].lower()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda s: s["tickers"].append("AAPL"),
        lambda s: s["companies"].__setitem__("AAPL", {**s["companies"]["AAPL"], "cik": ""}),
        lambda s: s["companies"].__setitem__("AAPL", {**s["companies"]["AAPL"], "cik": "abc"}),
        lambda s: s["companies"]["AAPL"].pop("legal_name"),
        lambda s: s["companies"]["AAPL"].pop("sector"),
        lambda s: s["companies"]["AAPL"].pop("peer_group"),
        lambda s: s["companies"]["AAPL"].pop("issuer_class"),
        lambda s: s["companies"]["AAPL"].pop("filing_form_profile"),
        lambda s: s.pop("source_policy_expectations"),
        lambda s: s.__setitem__("semantic_hash", "0" * 64),
    ],
)
def test_universe_spec_rejects_each_missing_or_drifting_field(tmp_path: Path, mutator):
    from catalyst_data.manifests.universe import load_universe_spec

    data = _load_spec_dict()
    mutator(data)
    path = tmp_path / "bad.spec.json"
    _write_spec(path, data)
    with pytest.raises(ValueError):
        load_universe_spec(path)


def test_runtime_universe_manifest_identity_is_deterministic():
    from catalyst_data.manifests.universe import build_universe_manifest, load_universe_spec

    spec = load_universe_spec(_spec_path())
    created = datetime(2026, 7, 24, tzinfo=timezone.utc)
    approved = datetime(2026, 7, 23, tzinfo=timezone.utc)
    kwargs = dict(
        discovery_artifact_hashes={"provider_coverage": "a" * 64},
        observed_provider_capabilities={"polygon_news": {"max_window_days": 7}},
        source_windows=_source_windows("2026-07-23"),
        coverage_status_summary={"polygon_news": "offline"},
        approved_at=approved,
        created_at=created,
    )
    m1 = build_universe_manifest(spec, **kwargs)
    m2 = build_universe_manifest(
        spec,
        **{**kwargs, "created_at": datetime(2026, 7, 24, 1, tzinfo=timezone.utc)},
    )
    m3 = build_universe_manifest(
        spec,
        **{**kwargs, "discovery_artifact_hashes": {"provider_coverage": "b" * 64}},
    )
    assert m1.runtime_manifest_id == m2.runtime_manifest_id
    assert m1.runtime_manifest_id != m3.runtime_manifest_id
    assert m1.tickers == RATIFIED_TICKERS


def test_source_windows_and_bounded_scopes_include_weekends_and_series_ids():
    from catalyst_data.manifests.universe import (
        build_b2o_source_scopes,
        load_universe_spec,
        terminal_complete,
    )
    from catalyst_data.pipeline.fred_manifest import FETCHED_SERIES

    windows = _source_windows("2025-08-04")
    with pytest.raises(ValueError):
        type(windows)(
            degraded_start="2025-08-02",
            degraded_end="2025-08-01",
            canonical_start="2025-08-01",
            canonical_end="2025-08-04",
        )
    spec = load_universe_spec(_spec_path())
    scopes = build_b2o_source_scopes(spec, windows)
    assert scopes["polygon_news"].request_window_days == 7
    assert scopes["finnhub_company_news"].request_window_days == 7
    assert scopes["polygon_ohlcv"].request_window_days == 90
    polygon_news = scopes["polygon_news"].expand_cells()
    assert any(c.window_start <= "2025-08-02" <= c.window_end for c in polygon_news)
    assert all((date.fromisoformat(c.window_end) - date.fromisoformat(c.window_start)).days <= 6 for c in polygon_news)
    assert len([c for c in polygon_news if c.subject == "AAPL"]) == 31
    ohlcv_windows = [c for c in scopes["polygon_ohlcv"].expand_cells() if c.subject == "AAPL"]
    assert all((date.fromisoformat(c.window_end) - date.fromisoformat(c.window_start)).days <= 89 for c in ohlcv_windows)
    assert scopes["fred_macro"].subjects == tuple(sorted(FETCHED_SERIES))
    assert {c.stage for c in scopes["sec_filings"].expand_cells()} == {"evidence"}
    assert terminal_complete("success", 1)
    assert terminal_complete("success_empty", 1)
    assert not terminal_complete("success_empty", 0)
    assert not terminal_complete("partial", 1)


def test_b2o_source_cell_identity_includes_endpoint_window_and_caps():
    from catalyst_data.manifests.universe import SourceCell

    cell = SourceCell.create(
        "evidence",
        "finnhub_company_news",
        "company-news",
        "AAPL",
        "2025-08-01",
        "2025-08-07",
        "calendar_days",
        "v1",
        page_cap=None,
        item_cap=250,
    )
    same = SourceCell.create(
        "evidence",
        "finnhub_company_news",
        "company-news",
        "AAPL",
        "2025-08-01",
        "2025-08-07",
        "calendar_days",
        "v1",
        page_cap=None,
        item_cap=250,
    )
    changed_end = SourceCell.create(
        "evidence",
        "finnhub_company_news",
        "company-news",
        "AAPL",
        "2025-08-01",
        "2025-08-06",
        "calendar_days",
        "v1",
        page_cap=None,
        item_cap=250,
    )
    changed_cap = SourceCell.create(
        "evidence",
        "finnhub_company_news",
        "company-news",
        "AAPL",
        "2025-08-01",
        "2025-08-07",
        "calendar_days",
        "v1",
        page_cap=None,
        item_cap=251,
    )
    identity = cell.to_identity()
    assert identity["endpoint_name"] == "company-news"
    assert identity["window_start"] == "2025-08-01"
    assert identity["window_end"] == "2025-08-07"
    assert identity["item_cap"] == 250
    assert cell.cell_id == same.cell_id
    assert cell.cell_id != changed_end.cell_id
    assert cell.cell_id != changed_cap.cell_id


def test_b2o_plan_universe_contains_exactly_ratified_40_and_fmp_three_endpoint_cells(tmp_path: Path):
    from catalyst_data.manifests.universe import build_b2o_source_scopes, load_universe_spec
    from catalyst_data.pipeline.fred_manifest import FETCHED_SERIES
    from catalyst_data.update_planner import plan_update

    db_path = _file_db(tmp_path)
    spec = load_universe_spec(_spec_path())
    scopes = build_b2o_source_scopes(spec, _source_windows("2025-08-01"))
    plan = plan_update(db_path, source_scopes=scopes, reference_today="2025-08-02")

    assert plan.universe["tickers"] == RATIFIED_TICKERS
    assert not set(FETCHED_SERIES).intersection(plan.universe["tickers"])
    fmp_cells = [
        cell for cell in plan.stages["evidence"]["cells"]
        if cell["source_type"] == "fmp_fundamentals"
    ]
    assert len(fmp_cells) == 40 * 3
    assert {cell["endpoint_name"] for cell in fmp_cells} == {
        "income_statement",
        "balance_sheet",
        "cash_flow",
    }
    fred_cells = [
        cell for cell in plan.stages["evidence"]["cells"]
        if cell["source_type"] == "fred_macro"
    ]
    assert {cell["endpoint_name"] for cell in fred_cells} == set(FETCHED_SERIES)


def test_b2o_planner_reuses_checkpoints_only_by_full_cell_id(tmp_path: Path):
    from catalyst_data.manifests.universe import SourceCell, SourceScope
    from catalyst_data.update_planner import plan_update

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    old_cell = SourceCell.create(
        "evidence", "polygon_news", "news", "AAPL",
        "2025-08-01", "2025-08-01", "calendar_days", "v1",
        page_cap=20, item_cap=1000,
    )
    conn.execute(
        """INSERT INTO source_checkpoints
           (run_id, source_type, ticker, date, status, logical_fetch_id,
            request_count, pages_received, items_received, is_complete,
            cell_id, window_start, window_end, endpoint_name, provider_profile_version)
           VALUES ('run-old', 'polygon_news', 'AAPL', '2025-08-01',
                   'success', ?, 1, 1, 0, 1, ?, ?, ?, ?, ?)""",
        ("x" * 64, old_cell.cell_id, old_cell.window_start, old_cell.window_end, old_cell.endpoint_name, old_cell.provider_profile_version),
    )
    conn.commit()
    conn.close()

    scopes = {
        "polygon_news": SourceScope(
            "polygon_news",
            ("AAPL",),
            "calendar_days",
            "2025-08-01",
            "2025-08-07",
            "evidence",
            7,
            endpoint_names=("news",),
            page_cap=20,
            item_cap=1000,
        )
    }
    plan = plan_update(db_path, source_scopes=scopes, reference_today="2025-08-08")
    assert [c["window_end"] for c in plan.stages["evidence"]["cells"]] == ["2025-08-07"]


def test_execute_propagates_full_7_day_cell_identity_to_transport_ledger_checkpoint_and_resume(tmp_path: Path):
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    cell = SourceCell.create(
        "evidence",
        "finnhub_company_news",
        "company-news",
        "AAPL",
        "2025-08-01",
        "2025-08-07",
        "calendar_days",
        "v1",
        page_cap=None,
        item_cap=250,
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["finnhub_company_news"], "tickers": RATIFIED_TICKERS},
        universe={"tickers": RATIFIED_TICKERS},
        stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash
    calls: list[dict] = []

    class FakeTransport:
        async def request(self, **kwargs):
            calls.append(kwargs)
            return {
                "status": 200,
                "body": json.dumps({"data": []}).encode(),
                "json": {"data": []},
            }

    first = asyncio.run(execute_update(db=conn, plan=plan, transport=FakeTransport()))
    assert calls[0]["window_start"] == "2025-08-01"
    assert calls[0]["window_end"] == "2025-08-07"
    assert calls[0]["endpoint_name"] == "company-news"
    assert first["cells_total"] == 1
    attempt = conn.execute(
        "SELECT logical_fetch_id, endpoint_name, ticker_or_series, window_start, window_end FROM provider_request_attempts"
    ).fetchone()
    checkpoint = conn.execute(
        "SELECT cell_id, endpoint_name, ticker, window_start, window_end, is_complete FROM source_checkpoints"
    ).fetchone()
    assert attempt[1:] == ("company-news", "AAPL", "2025-08-01", "2025-08-07")
    assert checkpoint == (cell.cell_id, "company-news", "AAPL", "2025-08-01", "2025-08-07", 1)
    second = asyncio.run(execute_update(db=conn, plan=plan, transport=FakeTransport(), parent_run_id=first["run_id"]))
    assert second["cells_skipped"] == 1
    assert second["cells_total"] == 0
    conn.close()


def test_readiness_fails_when_earlier_window_partial_even_if_later_succeeds(tmp_path: Path):
    import catalyst_data.coverage_audit as audit
    from catalyst_data.manifests.universe import SourceCell

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    partial = SourceCell.create("evidence", "polygon_news", "news", "AAPL", "2025-08-01", "2025-08-07", "calendar_days", "v1", page_cap=20, item_cap=1000)
    later = SourceCell.create("evidence", "polygon_news", "news", "AAPL", "2025-08-08", "2025-08-14", "calendar_days", "v1", page_cap=20, item_cap=1000)
    for cell, status, complete in ((partial, "partial", 0), (later, "success_empty", 1)):
        conn.execute(
            """INSERT INTO source_checkpoints
               (run_id, source_type, ticker, date, status, logical_fetch_id,
                request_count, pages_received, items_received, is_complete,
                cell_id, window_start, window_end, endpoint_name, provider_profile_version)
               VALUES (?, ?, ?, ?, ?, ?, 1, 1, 0, ?, ?, ?, ?, ?, ?)""",
            ("run", cell.source_type, cell.subject, cell.window_start, status, cell.cell_id, complete, cell.cell_id, cell.window_start, cell.window_end, cell.endpoint_name, cell.provider_profile_version),
        )
    conn.commit()
    conn.close()
    plan = SimpleNamespace(config={"source_scopes": [{
        "source_type": "polygon_news",
        "subjects": ["AAPL"],
        "date_domain": "calendar_days",
        "start_date": "2025-08-01",
        "end_date": "2025-08-14",
        "stage": "evidence",
        "request_window_days": 7,
        "provider_profile_version": "v1",
        "endpoint_names": ["news"],
        "page_cap": 20,
        "item_cap": 1000,
    }]})
    report = audit.run_b2o_readiness_audit(
        db_path,
        universe_manifest={"tickers": ["AAPL"]},
        plan=plan,
        terminal_run_id="legacy-test-run",
    )
    gate = report["b2o_readiness"]["canonical_news_comparable_gate"]
    assert gate["status"] == "incomplete"
    assert partial.cell_id in gate["missing_or_incomplete_cell_ids"]


def test_readiness_rejects_terminal_cell_without_request_raw_and_provenance(tmp_path: Path):
    import catalyst_data.coverage_audit as audit
    from catalyst_data.manifests.universe import SourceCell

    db_path = _file_db(tmp_path)
    cell = SourceCell.create(
        "evidence", "polygon_news", "news", "AAPL",
        "2025-08-01", "2025-08-01", "calendar_days", "v1",
        page_cap=20, item_cap=1000,
    )
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO ingestion_runs (run_id, plan_hash, expected_plan_hash, ticker_list_json, source_list_json, status, allow_stale_ohlcv, allow_stale_ohlcv_overridden, cancel_requested, started_at) VALUES ('run-no-provenance', '', '', '[]', '[]', 'PLANNED', 0, 0, 0, '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        """INSERT INTO source_checkpoints
           (run_id, source_type, ticker, date, status, logical_fetch_id,
            request_count, pages_received, items_received, is_complete,
            items_count, cell_id, window_start, window_end, endpoint_name,
            provider_profile_version)
           VALUES ('run-no-provenance', 'polygon_news', 'AAPL', '2025-08-01',
                   'success', ?, 1, 1, 1, 1, 1, ?, '2025-08-01',
                   '2025-08-01', 'news', 'v1')""",
        ("f" * 64, cell.cell_id),
    )
    conn.commit()
    conn.close()
    plan = SimpleNamespace(config={"source_scopes": [{
        "source_type": "polygon_news",
        "subjects": ["AAPL"],
        "date_domain": "calendar_days",
        "start_date": "2025-08-01",
        "end_date": "2025-08-01",
        "stage": "evidence",
        "request_window_days": 7,
        "provider_profile_version": "v1",
        "endpoint_names": ["news"],
        "page_cap": 20,
        "item_cap": 1000,
    }]})

    report = audit.run_b2o_readiness_audit(
        db_path, universe_manifest={"tickers": ["AAPL"]}, plan=plan,
        terminal_run_id="run-no-provenance"
    )["b2o_readiness"]

    assert report["required_provenance"]["status"] == "incomplete"
    assert report["required_provenance"]["missing_or_invalid_cell_ids"] == [cell.cell_id]
    assert report["overall_readiness"]["status"] == "incomplete"


def test_plan_update_source_scopes_hashes_windows_and_preserves_legacy(tmp_path: Path):
    from catalyst_data.manifests.universe import build_b2o_source_scopes, load_universe_spec
    from catalyst_data.update_planner import plan_update

    db_path = _file_db(tmp_path)
    legacy = plan_update(db_path, tickers=["AAPL"], sources=["polygon_news"], from_date="2025-08-01", to_date="2025-08-01", reference_today="2025-08-05")
    legacy2 = plan_update(db_path, tickers=["AAPL"], sources=["polygon_news"], from_date="2025-08-01", to_date="2025-08-01", reference_today="2025-08-05")
    assert legacy.plan_hash == legacy2.plan_hash
    assert "source_scopes" not in legacy.config

    spec = load_universe_spec(_spec_path())
    scopes = build_b2o_source_scopes(spec, _source_windows("2025-08-04"))
    scoped = plan_update(db_path, source_scopes=scopes, reference_today="2025-08-05")
    scoped_same_db_changed_hash_field = asdict(scoped)
    scoped_same_db_changed_hash_field["db_sha256"] = "0" * 64
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    assert compute_plan_hash(UpdatePlan(**scoped_same_db_changed_hash_field)) == scoped.plan_hash
    assert scoped.stages["market"]["cells"]
    assert scoped.stages["evidence"]["cells"]
    assert scoped.estimates["requests"]["polygon_news"] > 0
    changed = plan_update(
        db_path,
        source_scopes=build_b2o_source_scopes(spec, _source_windows("2025-08-05")),
        reference_today="2025-08-05",
    )
    assert changed.plan_hash != scoped.plan_hash


def test_execute_resume_lineage_skips_only_terminal_complete_cells(tmp_path: Path):
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    cell_ok = SourceCell.create("evidence", "polygon_news", "news", "AAPL", "2025-08-01", "2025-08-01", "calendar_days", "v1", page_cap=20, item_cap=1000)
    cell_partial = SourceCell.create("evidence", "polygon_news", "news", "AAPL", "2025-08-02", "2025-08-02", "calendar_days", "v1", page_cap=20, item_cap=1000)
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell_ok), asdict(cell_partial)]}},
    )
    from catalyst_data.update_planner import compute_plan_hash

    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    async def fake_transport(provider, method, url, **kwargs):
        return {"status": 200, "body": json.dumps({"results": []}).encode()}

    first = asyncio.run(execute_update(db=conn, plan=plan, transport=fake_transport))
    assert first["cells_total"] == 2
    conn.execute(
        "UPDATE source_checkpoints SET status='partial', is_complete=0 WHERE date='2025-08-02'"
    )
    conn.commit()
    second = asyncio.run(
        execute_update(db=conn, plan=plan, transport=fake_transport, parent_run_id=first["run_id"])
    )
    assert second["cells_skipped"] == 1
    assert second["cells_total"] == 1
    parent_ids = [r[0] for r in conn.execute("SELECT parent_run_id FROM ingestion_runs WHERE run_id=?", (second["run_id"],))]
    assert parent_ids == [first["run_id"]]
    conn.close()


def test_execute_stops_at_cell_boundary_when_runtime_budget_expires(tmp_path: Path):
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    cell = SourceCell.create(
        "evidence", "polygon_news", "news", "AAPL",
        "2025-08-01", "2025-08-01", "calendar_days", "v1",
        page_cap=20, item_cap=1000,
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    async def unexpected_transport(*args, **kwargs):
        raise AssertionError("expired runtime budget must stop before network")

    first = asyncio.run(
        execute_update(
            db=conn,
            plan=plan,
            transport=unexpected_transport,
            max_runtime_seconds=0,
        )
    )
    assert first["status"] == "CANCELLED"
    assert first["cells_total"] == 0
    assert first["stop_reason"] == "runtime_budget_exhausted"
    assert conn.execute(
        "SELECT status FROM ingestion_runs WHERE run_id=?", (first["run_id"],)
    ).fetchone()[0] == "CANCELLED"

    async def fake_transport(provider, method, url, **kwargs):
        return {"status": 200, "body": json.dumps({"results": []}).encode()}

    resumed = asyncio.run(
        execute_update(
            db=conn,
            plan=plan,
            transport=fake_transport,
            parent_run_id=first["run_id"],
        )
    )
    assert resumed["status"] == "SUCCEEDED"
    assert resumed["cells_total"] == 1
    conn.close()


def test_domain_materialization_for_finnhub_sec_fred_and_failure_status(tmp_path: Path):
    from catalyst_data.connectors.base import FetchResult
    from catalyst_data.update_pipeline import _record_b2_entity, _write_b2_checkpoint

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    raw_id = "raw:b2o"
    conn.execute(
        "INSERT OR REPLACE INTO raw_assets (asset_id,ticker,source_type,reference_date,fetched_at,content_raw,metadata_json) VALUES (?,?,?,?,?,?,?)",
        (raw_id, "AAPL", "finnhub_company_news", "2025-08-01", "2026-01-01T00:00:00Z", b"{}", "{}"),
    )
    finnhub_count = _record_b2_entity(
        conn,
        source="finnhub_company_news",
        ticker="AAPL",
        date="2025-08-01",
        data={"data": [{"id": "fh-1", "datetime": 1754006400, "headline": "AAPL news", "summary": "body", "url": "https://example.com", "source": "Finnhub"}]},
        raw_asset_id=raw_id,
    )
    assert finnhub_count == 1
    assert conn.execute("SELECT COUNT(*) FROM articles WHERE source_type='finnhub_company_news'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM article_tickers WHERE ticker='AAPL'").fetchone()[0] == 1

    sec_count = _record_b2_entity(
        conn,
        source="sec_filings",
        ticker="AAPL",
        date="2025-08-01",
        data={"filings": [{"filing_id": "sec:aapl:1", "cik": "0000320193", "form_type": "10-K", "filed_at": "2025-02-01", "accession_number": "0001", "document_url": "https://sec.test/doc", "text": "filing text"}]},
        raw_asset_id=raw_id,
    )
    assert sec_count == 1
    assert conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM filing_documents").fetchone()[0] == 1

    fred_count = _record_b2_entity(
        conn,
        source="fred_macro",
        ticker="DFF",
        date="2025-08-01",
        data={"id": "DFF", "observations": [{"date": "2025-08-01", "value": "4.5", "realtime_start": "2025-08-01"}]},
        raw_asset_id=raw_id,
    )
    assert fred_count == 1
    assert conn.execute("SELECT COUNT(*) FROM macro_observations WHERE series_id='DFF'").fetchone()[0] == 1

    _write_b2_checkpoint(
        conn,
        run_id="run-fail",
        source="polygon_news",
        ticker="AAPL",
        date="2025-08-01",
        status="failed",
        logical_fetch_id="x" * 64,
        items_count=0,
        http_status=429,
        is_complete=0,
    )
    assert conn.execute("SELECT status FROM source_checkpoints WHERE run_id='run-fail'").fetchone()[0] != "success_empty"
    conn.close()


def test_migration_v11_adds_full_cell_identity_and_fundamental_statements(tmp_path: Path):
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    from catalyst_data.migrations import CURRENT_SCHEMA_VERSION
    assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    checkpoint_cols = {row[1] for row in conn.execute("PRAGMA table_info(source_checkpoints)")}
    assert {"cell_id", "window_start", "window_end", "endpoint_name", "provider_profile_version"}.issubset(checkpoint_cols)
    fundamental_cols = {row[1] for row in conn.execute("PRAGMA table_info(fundamental_statements)")}
    assert {"statement_id", "raw_asset_id", "provider", "ticker", "statement_type", "fiscal_date", "payload_json"}.issubset(fundamental_cols)
    with pytest.raises(sqlite3.IntegrityError, match="b2o_checkpoint_cell_identity_contract"):
        conn.execute(
            """INSERT INTO source_checkpoints
               (run_id, source_type, ticker, date, status, logical_fetch_id,
                request_count, pages_received, items_received, is_complete, cell_id)
               VALUES ('r','polygon_news','AAPL','2025-08-01','success',?,1,1,0,1,?)""",
            ("x" * 64, "a" * 64),
        )
    conn.close()


def test_migration_v11_repairs_legacy_clean_asset_foreign_key(tmp_path: Path):
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("DROP VIEW corpus_items")
    conn.execute("DROP TABLE clean_assets")
    conn.execute(
        """CREATE TABLE clean_assets (
            asset_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            reference_date TEXT NOT NULL,
            cleaned_at TEXT NOT NULL,
            content_md TEXT NOT NULL,
            title_hash TEXT,
            is_duplicate INTEGER DEFAULT 0,
            is_rag_eligible INTEGER NOT NULL DEFAULT 1,
            is_canonical INTEGER NOT NULL DEFAULT 1,
            published_utc TEXT,
            raw_asset_id TEXT,
            FOREIGN KEY (asset_id) REFERENCES raw_assets(asset_id)
        )"""
    )
    conn.execute(
        """INSERT INTO raw_assets
           (asset_id,ticker,source_type,reference_date,fetched_at,content_raw)
           VALUES ('raw-1','AAPL','polygon_news','2025-08-01','2025-08-01',X'7B7D')"""
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        """INSERT INTO clean_assets
           (asset_id,ticker,source_type,reference_date,cleaned_at,content_md,raw_asset_id)
           VALUES ('clean-1','AAPL','polygon_news','2025-08-01','2025-08-01','content','raw-1')"""
    )
    conn.execute("PRAGMA user_version=6")
    conn.commit()

    run_migrations(conn)

    foreign_keys = conn.execute("PRAGMA foreign_key_list(clean_assets)").fetchall()
    assert [(row[3], row[2], row[4]) for row in foreign_keys] == [
        ("raw_asset_id", "raw_assets", "asset_id")
    ]
    assert conn.execute(
        "SELECT asset_id,raw_asset_id FROM clean_assets"
    ).fetchall() == [("clean-1", "raw-1")]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_migration_v11_rejects_update_into_invalid_or_mutated_cell_identity(tmp_path: Path):
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO source_checkpoints
           (run_id, source_type, ticker, date, status, logical_fetch_id,
            request_count, pages_received, items_received, is_complete)
           VALUES ('legacy', 'polygon_news', 'AAPL', '2025-08-01',
                   'pending', ?, 0, 0, 0, 0)""",
        ("x" * 64,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="b2o_checkpoint_cell_identity_contract"):
        conn.execute(
            "UPDATE source_checkpoints SET cell_id=?, endpoint_name='news' WHERE run_id='legacy'",
            ("a" * 64,),
        )

    conn.execute(
        """INSERT INTO source_checkpoints
           (run_id, source_type, ticker, date, status, logical_fetch_id,
            request_count, pages_received, items_received, is_complete,
            cell_id, window_start, window_end, endpoint_name,
            provider_profile_version)
           VALUES ('valid', 'polygon_news', 'AAPL', '2025-08-01',
                   'success_empty', ?, 1, 1, 0, 1, ?, '2025-08-01',
                   '2025-08-07', 'news', 'v1')""",
        ("y" * 64, "b" * 64),
    )
    with pytest.raises(sqlite3.IntegrityError, match="b2o_checkpoint_cell_identity_immutable"):
        conn.execute(
            "UPDATE source_checkpoints SET endpoint_name=NULL WHERE run_id='valid'"
        )
    conn.close()


def test_fmp_statement_endpoint_writes_canonical_domain_and_provenance(tmp_path: Path):
    from catalyst_data.update_pipeline import _record_b2_entity

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    raw_id = "raw:fmp"
    conn.execute(
        "INSERT OR REPLACE INTO raw_assets (asset_id,ticker,source_type,reference_date,fetched_at,content_raw,metadata_json) VALUES (?,?,?,?,?,?,?)",
        (raw_id, "RDDT", "fmp_fundamentals", "2025-08-01", "2026-01-01T00:00:00Z", b"{}", "{}"),
    )
    count = _record_b2_entity(
        conn,
        source="fmp_fundamentals",
        ticker="RDDT",
        date="2025-08-01",
        endpoint_name="income_statement",
        data={"statements": [{"date": "2024-12-31", "period": "FY", "reportedCurrency": "USD", "revenue": 1}]},
        raw_asset_id=raw_id,
    )
    assert count == 1
    row = conn.execute(
        "SELECT provider,ticker,statement_type,fiscal_date FROM fundamental_statements"
    ).fetchone()
    assert row == ("fmp", "RDDT", "income_statement", "2024-12-31")
    statement_id = conn.execute("SELECT statement_id FROM fundamental_statements").fetchone()[0]
    assert conn.execute(
        "SELECT COUNT(*) FROM normalized_provenance WHERE entity_type='fundamental_snapshot' AND entity_id=? AND raw_asset_id=?",
        (statement_id, raw_id),
    ).fetchone()[0] == 1
    conn.close()


def test_sec_cik_map_supports_ratified_40_new_tickers():
    from catalyst_data.cik_map import SUPPORTED_TICKERS, ticker_to_cik

    assert set(RATIFIED_TICKERS).issubset(SUPPORTED_TICKERS)
    for ticker in ("RDDT", "TSM", "ASML", "LCID", "RIVN", "CNC"):
        cik = ticker_to_cik(ticker)
        assert cik.isdigit() and len(cik) == 10


def test_bootstrap_candidate_uses_readonly_backup_and_preserves_source(tmp_path: Path):
    from catalyst_data.manifests.operations import ResourceLimitError, bootstrap_candidate, sha256_file

    source = _file_db(tmp_path)
    conn = sqlite3.connect(source)
    conn.execute("INSERT INTO ohlcv (symbol,date,open,high,low,close,volume) VALUES ('AAPL','2024-12-31',1,1,1,1,1)")
    conn.commit()
    conn.close()
    expected = sha256_file(source)
    candidate = tmp_path / "candidate.db"
    result = bootstrap_candidate(source_path=source, expected_source_sha256=expected, candidate_path=candidate, min_free_bytes=0)
    from catalyst_data.migrations import CURRENT_SCHEMA_VERSION
    assert result.user_version == CURRENT_SCHEMA_VERSION
    assert result.source_sha256_before == result.source_sha256_after == expected
    cconn = sqlite3.connect(candidate)
    assert cconn.execute("SELECT COUNT(*) FROM ohlcv WHERE date<'2025-01-02'").fetchone()[0] == 1
    assert cconn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    cconn.close()
    with pytest.raises(ValueError):
        bootstrap_candidate(source_path=source, expected_source_sha256="0" * 64, candidate_path=tmp_path / "bad.db", min_free_bytes=0)
    with pytest.raises(ResourceLimitError):
        bootstrap_candidate(source_path=source, expected_source_sha256=expected, candidate_path=tmp_path / "nofree.db", min_free_bytes=10**30)


def test_bootstrap_refuses_to_replace_an_existing_candidate(tmp_path: Path):
    from catalyst_data.manifests.operations import bootstrap_candidate, sha256_file

    source = _file_db(tmp_path)
    candidate = tmp_path / "candidate.db"
    candidate.write_bytes(b"existing-candidate")

    with pytest.raises(FileExistsError):
        bootstrap_candidate(
            source_path=source,
            expected_source_sha256=sha256_file(source),
            candidate_path=candidate,
            min_free_bytes=0,
        )

    assert candidate.read_bytes() == b"existing-candidate"


def test_resource_estimate_and_gate_match_binding_formula(tmp_path: Path):
    from catalyst_data.manifests.operations import (
        ResourceLimitError,
        check_publication_resources,
    )
    from catalyst_data.corpus.streaming_publication import (
        estimate_streaming_publication_resources,
    )

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO raw_assets (asset_id,ticker,source_type,reference_date,fetched_at,content_raw,metadata_json) VALUES ('raw-a','AAPL','polygon_news','2025-08-01','2026-01-01T00:00:00Z',x'7b7d','{}')"
    )
    conn.execute(
        "INSERT INTO articles (article_id,raw_asset_id,provider,source_type,ticker,reference_date,published_utc,title,description,article_url) VALUES ('a','raw-a','polygon','polygon_news','AAPL','2025-08-01','2025-08-01T00:00:00Z','abcd','ef','https://e.test')"
    )
    conn.execute(
        "INSERT INTO filings (filing_id,cik,ticker,form_type,filed_at,accession_number,url,raw_asset_id) VALUES ('f','0000320193','AAPL','10-K','2025-01-01','1','u','raw')"
    )
    conn.execute(
        "INSERT INTO filing_documents (filing_id,document_url,document_type,text,char_len,extraction_status,document_id) VALUES ('f','u','primary','x' || zeroblob(639),'640','success',?)",
        ("f" * 64,),
    )
    conn.commit()
    estimate = estimate_streaming_publication_resources(conn)
    assert estimate.eligible_document_count == 2
    assert estimate.source_utf8_bytes == len("abcd\nef".encode()) + 640
    assert estimate.largest_source_document_utf8_bytes == 640
    assert estimate.estimated_chunks == 2
    assert estimate.required_headroom == max(estimate.phase_headroom_bytes.values())
    with pytest.raises(ResourceLimitError):
        check_publication_resources(
            estimate,
            current_rss_bytes=6_000_000_000,
            free_disk_bytes=20 * 1024**3,
            protected_db_size=1024,
        )
    conn.close()


def test_snapshot_manifest_typed_hashing_and_identity_exclusions(tmp_path: Path):
    from catalyst_data.manifests.snapshot import build_legacy_data_snapshot_manifest as build_data_snapshot_manifest

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO ohlcv (symbol,date,open,high,low,close,volume) VALUES ('AAPL','2025-08-01',1,2,3,4,5)")
    conn.execute(
        "INSERT INTO raw_assets (asset_id,ticker,source_type,reference_date,fetched_at,content_raw,metadata_json) VALUES ('raw-type','AAPL','polygon_news','2025-08-01','2026-01-01T00:00:00Z',?, '{}')",
        (b"payload",),
    )
    conn.commit()
    windows = _source_windows("2025-08-01")
    created = datetime(2026, 7, 24, tzinfo=timezone.utc)
    m1 = build_data_snapshot_manifest(
        conn,
        universe_manifest_id="a" * 64,
        plan_hash="b" * 64,
        protected_source_sha256="c" * 64,
        source_windows=windows,
        coverage_states={"overall_readiness": {"status": "complete"}, "canonical_news_comparable_gate": {"status": "complete"}},
        created_at=created,
    )
    m2 = build_data_snapshot_manifest(
        conn,
        universe_manifest_id="a" * 64,
        plan_hash="b" * 64,
        protected_source_sha256="c" * 64,
        source_windows=windows,
        coverage_states={"overall_readiness": {"status": "complete"}, "canonical_news_comparable_gate": {"status": "complete"}},
        created_at=datetime(2026, 7, 24, 1, tzinfo=timezone.utc),
        report_path="/tmp/report.json",
    )
    assert m1.snapshot_id == m2.snapshot_id
    conn.execute("UPDATE raw_assets SET content_raw='payload' WHERE asset_id='raw-type'")
    conn.commit()
    m3 = build_data_snapshot_manifest(
        conn,
        universe_manifest_id="a" * 64,
        plan_hash="b" * 64,
        protected_source_sha256="c" * 64,
        source_windows=windows,
        coverage_states={"overall_readiness": {"status": "complete"}, "canonical_news_comparable_gate": {"status": "complete"}},
        created_at=created,
    )
    assert m3.snapshot_id != m1.snapshot_id
    assert "ohlcv" in m1.table_hashes
    conn.close()


def test_snapshot_table_hash_streams_rows_without_fetchall():
    from catalyst_data.manifests import snapshot

    assert ".fetchall()" not in inspect.getsource(snapshot._table_hash)


def test_snapshot_rejects_incomplete_readiness_and_wrong_user_version(tmp_path: Path):
    from catalyst_data.manifests.snapshot import build_legacy_data_snapshot_manifest as build_data_snapshot_manifest

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    with pytest.raises(ValueError, match="readiness"):
        build_data_snapshot_manifest(
            conn,
            universe_manifest_id="a" * 64,
            plan_hash="b" * 64,
            protected_source_sha256="c" * 64,
            source_windows=_source_windows("2025-08-01"),
            coverage_states={
                "overall_readiness": {"status": "incomplete"},
                "canonical_news_comparable_gate": {"status": "complete"},
            },
            created_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
        )
    conn.execute("PRAGMA user_version = 10")
    with pytest.raises(ValueError, match="user_version"):
        build_data_snapshot_manifest(
            conn,
            universe_manifest_id="a" * 64,
            plan_hash="b" * 64,
            protected_source_sha256="c" * 64,
            source_windows=_source_windows("2025-08-01"),
            coverage_states={
                "overall_readiness": {"status": "complete"},
                "canonical_news_comparable_gate": {"status": "complete"},
            },
            created_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
        )
    conn.close()


def test_publish_rejects_snapshot_from_different_db(tmp_path: Path, capsys):
    from catalyst_data.b2o import main
    from catalyst_data.manifests.snapshot import build_legacy_data_snapshot_manifest as build_data_snapshot_manifest

    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    db1 = _file_db(one)
    db2 = _file_db(two)
    conn1 = sqlite3.connect(db1)
    snap = build_data_snapshot_manifest(
        conn1,
        universe_manifest_id="a" * 64,
        plan_hash="b" * 64,
        protected_source_sha256="c" * 64,
        source_windows=_source_windows("2025-08-01"),
        coverage_states={"overall_readiness": {"status": "complete"}, "canonical_news_comparable_gate": {"status": "complete"}},
        created_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )
    conn1.close()
    conn2 = sqlite3.connect(db2)
    conn2.execute("INSERT INTO ohlcv (symbol,date,open,high,low,close,volume) VALUES ('AAPL','2025-08-01',1,1,1,1,1)")
    conn2.commit()
    conn2.close()
    snap_path = tmp_path / "snapshot.json"
    snap_path.write_text(json.dumps(snap.to_dict(), sort_keys=True, separators=(",", ":"), default=str))
    rc = main(["publish-corpus", "--db", str(db2), "--snapshot-manifest", str(snap_path)])
    assert rc == 7
    assert "does not match current DB" in json.loads(capsys.readouterr().out)["errors"][0]


def test_promote_rejects_missing_corpus_state_and_preserves_pointer(tmp_path: Path, capsys):
    from catalyst_data.b2o import main
    from catalyst_data.manifests.snapshot import build_legacy_data_snapshot_manifest as build_data_snapshot_manifest

    db = _file_db(tmp_path)
    conn = sqlite3.connect(db)
    snap = build_data_snapshot_manifest(
        conn,
        universe_manifest_id="a" * 64,
        plan_hash="b" * 64,
        protected_source_sha256="c" * 64,
        source_windows=_source_windows("2025-08-01"),
        coverage_states={"overall_readiness": {"status": "complete"}, "canonical_news_comparable_gate": {"status": "complete"}},
        created_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )
    conn.close()
    snap_path = tmp_path / "snapshot.json"
    snap_path.write_text(json.dumps(snap.to_dict(), sort_keys=True, separators=(",", ":"), default=str))
    pointer = tmp_path / "active.json"
    pointer.write_text('{"snapshot_id":"old"}')
    rc = main([
        "promote", "--db", str(db), "--snapshot-manifest", str(snap_path),
        "--snapshots-dir", str(tmp_path / "snapshots"), "--active-pointer", str(pointer),
    ])
    assert rc == 7
    assert "missing current corpus manifest" in json.loads(capsys.readouterr().out)["errors"][0]
    assert json.loads(pointer.read_text())["snapshot_id"] == "old"


def test_b2o_readiness_audit_calls_existing_once_and_detects_uncovered(tmp_path: Path, monkeypatch):
    import catalyst_data.coverage_audit as audit
    from catalyst_data.manifests.universe import build_b2o_source_scopes, build_universe_manifest, load_universe_spec

    db_path = _file_db(tmp_path)
    spec = load_universe_spec(_spec_path())
    windows = _source_windows("2025-08-01")
    manifest = build_universe_manifest(
        spec,
        discovery_artifact_hashes={"provider": "a" * 64},
        observed_provider_capabilities={},
        source_windows=windows,
        coverage_status_summary={},
        approved_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
        created_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )
    calls = {"n": 0}

    def fake_existing(db_path_arg, output_dir=None):
        calls["n"] += 1
        return {"per_source_table_counts": {}, "date_coverage": {}, "checkpoint_reconciliation": {}}

    monkeypatch.setattr(audit, "run_coverage_audit", fake_existing)
    report = audit.run_b2o_readiness_audit(
        db_path,
        universe_manifest=manifest,
        plan=SimpleNamespace(config={"source_scopes": {k: v.to_identity() for k, v in build_b2o_source_scopes(spec, windows).items()}}),
        terminal_run_id="legacy-test-run",
    )
    assert calls["n"] == 1
    assert "b2o_readiness" in report
    assert report["b2o_readiness"]["comparable_gate"]["status"] == "incomplete"
    assert report["b2o_readiness"]["uncovered_ranges"]


def test_live_transport_dispatches_fake_clients_and_authorized_cli_can_execute(tmp_path: Path, monkeypatch, capsys):
    from catalyst_data.b2o import ProviderCredentials, create_b2o_live_transport, main
    from catalyst_data.config import RatePolicy
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    class FakeClient:
        def __init__(self, payload):
            self.payload = payload
            self.calls = []
            self.closed = False

        async def get(self, url, params=None, headers=None):
            self.calls.append((url, params or {}, headers or {}))
            return SimpleNamespace(
                status_code=200,
                headers={},
                text=json.dumps(self.payload),
                content=json.dumps(self.payload).encode(),
                json=lambda: self.payload,
            )

        async def aclose(self):
            self.closed = True

    polygon = FakeClient({"results": [], "status": "OK"})
    async def exercise():
        async with create_b2o_live_transport(
            credentials=ProviderCredentials(polygon="poly-secret", finnhub="fh-secret", fred="fred-secret", fmp="fmp-secret", sec_user_agent="test@example.com"),
            rate_policies={"polygon": RatePolicy(0, 1, None)},
            request_caps={"polygon_news": 20},
            clients={"polygon": polygon},
        ) as transport:
            result = await transport.request(
                provider="polygon",
                source_type="polygon_news",
                endpoint_name="news",
                subject="AAPL",
                window_start="2025-08-01",
                window_end="2025-08-07",
            )
            assert result.status == 200
    asyncio.run(exercise())
    assert polygon.calls[0][1]["published_utc.lt"] == "2025-08-08T00:00:00Z"
    assert "apiKey" in polygon.calls[0][1]
    assert "poly-secret" not in repr(create_b2o_live_transport(
        credentials=ProviderCredentials(polygon="poly-secret", finnhub="fh-secret", fred="fred-secret", fmp="fmp-secret", sec_user_agent="test@example.com"),
        rate_policies={}, request_caps={}, clients={"polygon": polygon},
    ))

    db_path = _file_db(tmp_path)
    cell = SourceCell.create("evidence", "finnhub_company_news", "company-news", "AAPL", "2025-08-01", "2025-08-07", "calendar_days", "v1", item_cap=250)
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["finnhub_company_news"], "tickers": RATIFIED_TICKERS},
        universe={"tickers": RATIFIED_TICKERS},
        stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(asdict(plan), default=str))
    class FakeTransportContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, **kwargs):
            return {"status": 200, "body": b'{"data":[]}'}

    factory_calls = []

    def fake_factory(**kwargs):
        factory_calls.append(kwargs)
        return FakeTransportContext()

    monkeypatch.setattr("catalyst_data.b2o.create_b2o_live_transport", fake_factory)
    rc = main([
        "execute", "--db", str(db_path), "--plan", str(plan_path),
        "--expected-plan-hash", plan.plan_hash, "--authorized",
    ])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["status"] == "SUCCEEDED"
    assert factory_calls[0]["env_path"].name == ".env"
    assert {"polygon", "finnhub", "fred", "sec"}.issubset(
        factory_calls[0]["rate_policies"]
    )


def test_b2o_cli_does_not_expose_offline_success_fixture():
    from catalyst_data.b2o import main

    with pytest.raises(SystemExit):
        main(["execute", "--offline-success-empty-fixture"])


def test_polygon_window_pagination_persists_each_page_and_marks_complete(tmp_path: Path):
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    cell = SourceCell.create(
        "evidence", "polygon_news", "news", "AAPL",
        "2025-08-01", "2025-08-07", "calendar_days", "v1",
        page_cap=3, item_cap=10,
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash
    calls = []

    class FakeTransport:
        async def request(self, **kwargs):
            calls.append(kwargs)
            if kwargs["page_url"] is None:
                return {
                    "status": 200,
                    "body": json.dumps({
                        "results": [{"id": "p1", "title": "one", "published_utc": "2025-08-01T12:00:00Z"}],
                        "next_url": "https://api.polygon.io/v2/reference/news?cursor=next",
                    }).encode(),
                }
            return {
                "status": 200,
                "body": json.dumps({
                    "results": [{"id": "p2", "title": "two", "published_utc": "2025-08-02T12:00:00Z"}],
                }).encode(),
            }

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=FakeTransport()))

    assert report["status"] == "SUCCEEDED"
    assert [call["page_url"] for call in calls] == [
        None,
        "https://api.polygon.io/v2/reference/news?cursor=next",
    ]
    assert conn.execute("SELECT COUNT(*) FROM provider_request_attempts").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM raw_assets WHERE request_id IS NOT NULL").fetchone()[0] == 2
    checkpoint = conn.execute(
        """SELECT status, request_count, pages_received, items_received, is_complete
           FROM source_checkpoints WHERE cell_id=?""",
        (cell.cell_id,),
    ).fetchone()
    assert checkpoint == ("success", 2, 2, 2, 1)
    conn.close()


def test_b2o_retry_attempts_are_individually_ledgered_before_success(tmp_path: Path):
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    cell = SourceCell.create(
        "evidence", "polygon_news", "news", "AAPL",
        "2025-08-01", "2025-08-01", "calendar_days", "v1",
        page_cap=2, item_cap=10,
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    class RetryTransport:
        def __init__(self):
            self.calls = 0

        async def retry_sleep(self, delay):
            return None

        async def request(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"status": 429, "body": b'{"error":"rate limited"}'}
            return {"status": 200, "body": b'{"results":[]}'}

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=RetryTransport()))

    assert report["status"] == "SUCCEEDED"
    assert conn.execute(
        "SELECT attempt_no, status FROM provider_request_attempts ORDER BY attempt_no"
    ).fetchall() == [(1, "RATE_LIMITED"), (2, "SUCCEEDED")]
    assert conn.execute(
        "SELECT request_count, pages_received, is_complete FROM source_checkpoints"
    ).fetchone() == (2, 1, 1)
    assert conn.execute(
        "SELECT COUNT(*) FROM raw_assets WHERE request_id IS NOT NULL"
    ).fetchone()[0] == 2
    conn.close()


def test_b2o_transport_exception_never_persists_secret_bearing_message(tmp_path: Path):
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    cell = SourceCell.create(
        "market", "polygon_ohlcv", "ohlcv", "AAPL",
        "2025-08-01", "2025-08-01", "trading_sessions", "v1",
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["polygon_ohlcv"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": [asdict(cell)]}, "evidence": {"cells": []}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    class FailingTransport:
        async def request(self, **kwargs):
            raise RuntimeError("request failed apiKey=super-secret")

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=FailingTransport()))

    assert report["status"] == "FAILED"
    message = conn.execute(
        "SELECT error_message_redacted FROM provider_request_attempts"
    ).fetchone()[0]
    assert "super-secret" not in message
    assert "apiKey" not in message
    conn.close()


def test_polygon_ohlcv_window_materializes_every_returned_session(tmp_path: Path):
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    cell = SourceCell.create(
        "market", "polygon_ohlcv", "ohlcv", "AAPL",
        "2025-08-01", "2025-08-04", "trading_sessions", "v1",
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["polygon_ohlcv"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": [asdict(cell)]}, "evidence": {"cells": []}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    class FakeTransport:
        async def request(self, **kwargs):
            return {
                "status": 200,
                "body": json.dumps({"results": [
                    {"t": 1754006400000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10},
                    {"t": 1754265600000, "o": 2, "h": 3, "l": 1.5, "c": 2.5, "v": 20},
                ]}).encode(),
            }

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=FakeTransport()))

    assert report["status"] == "SUCCEEDED"
    assert conn.execute(
        "SELECT date FROM ohlcv WHERE symbol='AAPL' ORDER BY date"
    ).fetchall() == [("2025-08-01",), ("2025-08-04",)]
    assert conn.execute(
        "SELECT COUNT(*) FROM normalized_provenance WHERE entity_type='ohlcv'"
    ).fetchone()[0] == 2
    conn.close()


def test_combined_publication_and_promotion_simulation(tmp_path: Path):
    from catalyst_data.manifests.operations import promote_candidate, publish_corpus_with_resource_gate

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    result = publish_corpus_with_resource_gate(conn, snapshot_id="s" * 64, clock=lambda: "2026-07-24T00:00:00Z", min_free_bytes=0, max_rss_bytes=10**12)
    assert result.corpus.manifest_id
    assert result.lexical.manifest_id == result.corpus.manifest_id
    assert conn.execute("SELECT corpus_manifest_id FROM lexical_index_state").fetchone()[0] == result.corpus.manifest_id
    conn.close()

    candidate = tmp_path / f"catalyst_b2o_{'s'*64}.candidate.db"
    db_path.replace(candidate)
    promoted = promote_candidate(
        candidate_path=candidate,
        snapshot_id="s" * 64,
        snapshots_dir=tmp_path / "snapshots",
        active_pointer_path=tmp_path / "manifests" / "active_data_snapshot.json",
    )
    assert promoted.final_path.exists()
    assert json.loads(promoted.active_pointer_path.read_text())["snapshot_id"] == "s" * 64
    with pytest.raises(FileExistsError):
        promote_candidate(
            candidate_path=promoted.final_path,
            snapshot_id="s" * 64,
            snapshots_dir=tmp_path / "snapshots",
            active_pointer_path=tmp_path / "manifests" / "active_data_snapshot.json",
        )


def test_cli_contract_manifest_and_execute_guard(tmp_path: Path, capsys):
    from catalyst_data.b2o import main

    out = tmp_path / "manifest.json"
    rc = main([
        "manifest",
        "--spec",
        str(_spec_path()),
        "--output",
        str(out),
        "--latest-complete-session",
        "2025-08-01",
    ])
    assert rc == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["schema_version"] == "1.0.0"
    assert envelope["command"] == "manifest"
    assert envelope["status"] == "SUCCEEDED"
    assert out.exists()
    rc = main(["execute", "--db", str(tmp_path / "missing.db"), "--plan", str(tmp_path / "plan.json"), "--expected-plan-hash", "a" * 64])
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["status"] == "FAILED"


def test_live_transport_redacts_keys_and_rejects_missing_mandatory(monkeypatch, tmp_path: Path):
    from catalyst_data.b2o import create_b2o_live_transport
    from catalyst_data.config import RatePolicy

    for key in ("POLYGON_API_KEY", "FINNHUB_API_KEY", "FRED_API_KEY", "SEC_USER_AGENT"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError):
        create_b2o_live_transport(env_path=tmp_path / ".env", rate_policies={"polygon": RatePolicy(0, 1, None)}, request_caps={})
    monkeypatch.setenv("POLYGON_API_KEY", "secret-polygon")
    monkeypatch.setenv("FINNHUB_API_KEY", "secret-finnhub")
    monkeypatch.setenv("FRED_API_KEY", "secret-fred")
    with pytest.raises(ValueError, match="sec_user_agent"):
        create_b2o_live_transport(
            env_path=tmp_path / ".env", rate_policies={}, request_caps={}
        )
    monkeypatch.setenv("SEC_USER_AGENT", "Catalyst test@example.com")
    transport_cm = create_b2o_live_transport(env_path=tmp_path / ".env", rate_policies={}, request_caps={})
    assert "secret" not in repr(transport_cm)


def test_authorized_cli_returns_nonzero_when_execution_is_partial(tmp_path: Path, monkeypatch, capsys):
    from catalyst_data.b2o import main
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    db_path = _file_db(tmp_path)
    cell = SourceCell.create(
        "market", "polygon_ohlcv", "ohlcv", "AAPL",
        "2025-08-01", "2025-08-01", "trading_sessions", "v1",
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["polygon_ohlcv"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": [asdict(cell)]}, "evidence": {"cells": []}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(asdict(plan)))

    class FailingTransport:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, **kwargs):
            return {"status": 429, "body": b'{"error":"rate limited"}'}

    monkeypatch.setattr(
        "catalyst_data.b2o.create_b2o_live_transport",
        lambda **kwargs: FailingTransport(),
    )

    rc = main([
        "execute", "--db", str(db_path), "--plan", str(plan_path),
        "--expected-plan-hash", plan.plan_hash, "--authorized",
    ])

    envelope = json.loads(capsys.readouterr().out)
    assert rc != 0
    assert envelope["status"] == "FAILED"
    assert envelope["data"]["status"] == "FAILED"


def test_live_transport_enforces_rate_budget_and_rejects_untrusted_polygon_cursor():
    from catalyst_data.b2o import ProviderCredentials, create_b2o_live_transport
    from catalyst_data.config import RatePolicy
    from catalyst_data.rate_limiter import DailyBudgetExhausted

    class FakeClient:
        async def get(self, url, params=None, headers=None):
            return SimpleNamespace(
                status_code=200,
                headers={},
                text='{"results":[]}',
                json=lambda: {"results": []},
            )

    async def exercise():
        transport = create_b2o_live_transport(
            credentials=ProviderCredentials(
                polygon="p", finnhub="f", fred="r", sec_user_agent="test@example.com"
            ),
            rate_policies={"polygon": RatePolicy(0, 1, 1)},
            request_caps={},
            clients={"polygon": FakeClient()},
        )
        async with transport:
            await transport.request(
                provider="polygon",
                source_type="polygon_news",
                endpoint_name="news",
                subject="AAPL",
                window_start="2025-08-01",
                window_end="2025-08-07",
            )
            with pytest.raises(DailyBudgetExhausted):
                await transport.request(
                    provider="polygon",
                    source_type="polygon_news",
                    endpoint_name="news",
                    subject="AAPL",
                    window_start="2025-08-01",
                    window_end="2025-08-07",
                )

        fresh = create_b2o_live_transport(
            credentials=ProviderCredentials(
                polygon="p", finnhub="f", fred="r", sec_user_agent="test@example.com"
            ),
            rate_policies={},
            request_caps={},
            clients={"polygon": FakeClient()},
        )
        async with fresh:
            with pytest.raises(ValueError, match="Polygon pagination URL"):
                await fresh.request(
                    provider="polygon",
                    source_type="polygon_news",
                    endpoint_name="news",
                    subject="AAPL",
                    window_start="2025-08-01",
                    window_end="2025-08-07",
                    page_url="https://attacker.example/steal",
                )

    asyncio.run(exercise())


def test_live_transport_preserves_non_json_http_error_body_for_ledger(tmp_path: Path):
    from catalyst_data.b2o import ProviderCredentials, create_b2o_live_transport
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    class FakeClient:
        async def get(self, url, params=None, headers=None):
            def invalid_json():
                raise ValueError("not json")

            return SimpleNamespace(
                status_code=403,
                headers={},
                text="forbidden",
                content=b"forbidden",
                json=invalid_json,
            )

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    cell = SourceCell.create(
        "market", "polygon_ohlcv", "ohlcv", "AAPL",
        "2025-08-01", "2025-08-01", "trading_sessions", "v1",
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["polygon_ohlcv"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": [asdict(cell)]}, "evidence": {"cells": []}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    async def exercise():
        transport = create_b2o_live_transport(
            credentials=ProviderCredentials(
                polygon="p", finnhub="f", fred="r", sec_user_agent="test@example.com"
            ),
            rate_policies={},
            request_caps={},
            clients={"polygon": FakeClient()},
        )
        async with transport:
            return await execute_update(db=conn, plan=plan, transport=transport)

    report = asyncio.run(exercise())

    assert report["status"] == "FAILED"
    assert conn.execute(
        "SELECT status FROM provider_request_attempts"
    ).fetchone()[0] == "AUTH_ERROR"
    assert conn.execute(
        "SELECT content_raw FROM raw_assets WHERE request_id IS NOT NULL"
    ).fetchone()[0] == b"forbidden"
    conn.close()


def test_live_transport_reuses_and_closes_one_owned_client_per_provider(monkeypatch):
    from catalyst_data.b2o import ProviderCredentials, create_b2o_live_transport

    created = []

    class FakeOwnedClient:
        def __init__(self, **kwargs):
            self.closed = False
            created.append(self)

        async def get(self, url, params=None, headers=None):
            return SimpleNamespace(
                status_code=200,
                headers={},
                text='{"results":[]}',
                content=b'{"results":[]}',
                json=lambda: {"results": []},
            )

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr("httpx.AsyncClient", FakeOwnedClient)

    async def exercise():
        transport = create_b2o_live_transport(
            credentials=ProviderCredentials(
                polygon="p", finnhub="f", fred="r", sec_user_agent="test@example.com"
            ),
            rate_policies={},
            request_caps={},
        )
        async with transport:
            for _ in range(2):
                await transport.request(
                    provider="polygon",
                    source_type="polygon_news",
                    endpoint_name="news",
                    subject="AAPL",
                    window_start="2025-08-01",
                    window_end="2025-08-07",
                )

    asyncio.run(exercise())
    assert len(created) == 1
    assert created[0].closed


def test_fred_live_transport_requests_configured_series_history_without_as_of_day_filter():
    from catalyst_data.b2o import ProviderCredentials, create_b2o_live_transport

    class FakeClient:
        def __init__(self):
            self.params = None

        async def get(self, url, params=None, headers=None):
            self.params = params
            return SimpleNamespace(
                status_code=200,
                headers={},
                text='{"observations":[]}',
                json=lambda: {"observations": []},
            )

    client = FakeClient()

    async def exercise():
        transport = create_b2o_live_transport(
            credentials=ProviderCredentials(
                polygon="p", finnhub="f", fred="r", sec_user_agent="test@example.com"
            ),
            rate_policies={},
            request_caps={},
            clients={"fred": client},
        )
        async with transport:
            await transport.request(
                provider="fred",
                source_type="fred_macro",
                endpoint_name="DFF",
                subject="DFF",
                window_start="2026-07-23",
                window_end="2026-07-23",
            )

    asyncio.run(exercise())
    assert "observation_start" not in client.params
    assert "observation_end" not in client.params


def test_finnhub_item_cap_cannot_be_reported_as_complete(tmp_path: Path):
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    cell = SourceCell.create(
        "evidence", "finnhub_company_news", "company-news", "AAPL",
        "2025-08-01", "2025-08-07", "calendar_days", "v1", item_cap=2,
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["finnhub_company_news"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    class FakeTransport:
        async def request(self, **kwargs):
            return {
                "status": 200,
                "body": json.dumps([
                    {"id": "1", "headline": "one"},
                    {"id": "2", "headline": "two"},
                    {"id": "3", "headline": "three"},
                ]).encode(),
            }

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=FakeTransport()))

    assert report["status"] == "FAILED"
    assert conn.execute(
        "SELECT status, items_count, is_complete FROM source_checkpoints"
    ).fetchone() == ("partial", 2, 0)
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 2
    conn.close()


def test_sec_submissions_payload_materializes_supported_filing_rows(tmp_path: Path):
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    cell = SourceCell.create(
        "evidence", "sec_filings", "sec_submissions", "AAPL",
        "2026-07-23", "2026-07-23", "as_of", "v1",
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["sec_filings"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    class FakeTransport:
        async def request(self, **kwargs):
            return {
                "status": 200,
                "body": json.dumps({
                    "cik": "0000320193",
                    "filings": {"recent": {
                        "form": ["10-K", "S-8"],
                        "filingDate": ["2026-02-01", "2026-02-02"],
                        "accessionNumber": ["0000320193-26-000001", "0000320193-26-000002"],
                        "primaryDocument": ["aapl-2025.htm", "s8.htm"],
                        "reportDate": ["2025-12-31", "2026-02-01"],
                        "items": ["", ""],
                    }},
                }).encode(),
            }

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=FakeTransport()))

    assert report["status"] == "SUCCEEDED"
    assert conn.execute(
        "SELECT form_type, accession_number FROM filings"
    ).fetchall() == [("10-K", "0000320193-26-000001")]
    assert conn.execute(
        "SELECT COUNT(*) FROM normalized_provenance WHERE entity_type='filing'"
    ).fetchone()[0] == 1
    conn.close()


def test_b2o_offline_flow_runs_end_to_end_without_network(tmp_path: Path, monkeypatch):
    from catalyst_data.b2o import main
    from catalyst_data.manifests.operations import sha256_file

    def block_socket(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr("socket.socket", block_socket)
    source = _file_db(tmp_path)
    source_sha = sha256_file(source)
    bootstrap_db = tmp_path / "bootstrap.db"
    manifest_path = tmp_path / "universe.runtime.json"
    plan_path = tmp_path / "plan.json"
    snapshot_path = tmp_path / "snapshot.json"

    assert main(["manifest", "--spec", str(_spec_path()), "--output", str(manifest_path), "--latest-complete-session", "2025-08-01"]) == 0
    assert main(["bootstrap", "--source-db", str(source), "--expected-source-sha256", source_sha, "--bootstrap-db", str(bootstrap_db)]) == 0
    assert main(["plan", "--db", str(bootstrap_db), "--universe-manifest", str(manifest_path), "--latest-complete-session", "2025-08-01", "--output", str(plan_path)]) == 0
    conn = sqlite3.connect(str(bootstrap_db))
    conn.execute("INSERT INTO ingestion_runs (run_id, plan_hash, expected_plan_hash, ticker_list_json, source_list_json, status, allow_stale_ohlcv, allow_stale_ohlcv_overridden, cancel_requested, started_at) VALUES ('offline-run', '', '', '[]', '[]', 'PLANNED', 0, 0, 0, '2026-01-01T00:00:00Z')")
    conn.commit()
    conn.close()
    assert main(["audit", "--db", str(bootstrap_db), "--universe-manifest", str(manifest_path), "--plan", str(plan_path), "--output-dir", str(tmp_path), "--terminal-run-id", "offline-run"]) in {2, 5}
    assert main(["snapshot", "--db", str(bootstrap_db), "--universe-manifest", str(manifest_path), "--plan", str(plan_path), "--protected-source-sha256", source_sha, "--output", str(snapshot_path), "--terminal-run-id", "offline-run"]) in {2, 5}
    assert not snapshot_path.exists()


def test_static_no_model_gpu_secret_or_alternate_paths():
    changed_roots = [
        _repo_root() / "packages" / "data-core" / "catalyst_data" / "b2o.py",
        _repo_root() / "packages" / "data-core" / "catalyst_data" / "manifests",
        _repo_root() / "packages" / "data-core" / "catalyst_data" / "update_planner.py",
        _repo_root() / "packages" / "data-core" / "catalyst_data" / "update_pipeline.py",
        _repo_root() / "packages" / "data-core" / "catalyst_data" / "coverage_audit.py",
    ]
    haystack = "\n".join(
        p.read_text(errors="ignore")
        for root in changed_roots
        for p in ([root] if root.is_file() else root.rglob("*.py"))
        if "__pycache__" not in str(p) and p.name != "test_b2o_data_readiness.py"
    )
    forbidden = [
        "from catalyst_eval",
        "import catalyst_eval",
        "HuggingFace",
        "AutoModel",
        "SentenceTransformer",
        "cuda",
        "LexicalIndexManifest",
        "POLYGON_API_KEY=",
        "FINNHUB_API_KEY=",
        "FRED_API_KEY=",
    ]
    for needle in forbidden:
        assert needle not in haystack

# === FINAL CLEANUP TESTS ===
import ast
from pathlib import Path as AstPath

def test_readiness_rejects_missing_terminal_run_id():
    """run_b2o_readiness_audit() without terminal_run_id must raise TypeError."""
    import catalyst_data.coverage_audit as audit
    with pytest.raises(TypeError):
        audit.run_b2o_readiness_audit("/nonexistent", universe_manifest={"tickers": []})


def test_readiness_rejects_empty_terminal_run_id():
    """run_b2o_readiness_audit() with empty terminal_run_id must raise ValueError."""
    import catalyst_data.coverage_audit as audit
    with pytest.raises(ValueError, match="terminal_run_id"):
        audit.run_b2o_readiness_audit("/nonexistent", universe_manifest={"tickers": []}, terminal_run_id="")


def test_report_dict_has_no_duplicate_keys_ast():
    """AST inspection: execute_update report dict has no duplicate string keys."""
    import catalyst_data.update_pipeline as up
    source = AstPath(up.__file__).read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "execute_update":
            for child in ast.walk(node):
                if isinstance(child, ast.Dict) and len(child.keys) > 10:
                    string_keys = [k.value for k in child.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                    seen = set()
                    duplicates = [sk for sk in string_keys if sk in seen or seen.add(sk)]
                    assert len(duplicates) == 0, f"duplicate report keys: {duplicates}"
                    assert string_keys.count("missing_cells") == 1
                    assert string_keys.count("stop_reason") == 1
                    return
    pytest.fail("execute_update report dict not found")

# === RECOVERED OPERATIONAL SAFETY TESTS (Round 1-3) ===

# -- Helpers --
def _b2o_make_plan(cells_market, cells_evidence=None, tickers=None, sources=None):
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash
    sources = sources or [c.source_type for c in cells_market + (cells_evidence or [])]
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": sources, "tickers": tickers or ["AAPL"]},
        universe={"tickers": tickers or ["AAPL"]},
        stages={
            "market": {"cells": [asdict(c) for c in cells_market]},
            "evidence": {"cells": [asdict(c) for c in (cells_evidence or [])]},
        },
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash
    return plan

def _b2o_make_cell(source, endpoint, ticker, start, end, stage="market", page_cap=None, item_cap=None):
    from catalyst_data.manifests.universe import SourceCell
    domain = "trading_sessions" if source == "polygon_ohlcv" else "calendar_days"
    return SourceCell.create(stage, source, endpoint, ticker, start, end, domain, "v1", page_cap=page_cap, item_cap=item_cap)

def _b2o_ohlcv_cells(n=3):
    from catalyst_data.manifests.universe import SourceCell
    return [SourceCell.create("market", "polygon_ohlcv", "ohlcv", "AAPL", f"2026-01-{12+i:02d}", f"2026-01-{12+i:02d}", "trading_sessions", "v1") for i in range(n)]

def _b2o_success_response():
    class R:
        status_code = 200
        def json(self): return {"results": [{"o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000}], "next_url": None}
    return R()

def _b2o_transport_error():
    raise ConnectionError("simulated transport failure")

def _b2o_news_success():
    class R:
        status_code = 200
        def json(self): return {"results": [{"id": "a1", "title": "T", "published_utc": "2026-01-14T00:00:00Z", "article_url": "https://x/a", "tickers": ["AAPL"], "publisher": {"name": "P"}}], "next_url": None}
    return R()


# === A. Market Gate ===

def test_market_failure_blocks_evidence(tmp_path: Path):
    from catalyst_data.update_pipeline import execute_update
    cells = _b2o_ohlcv_cells(1)
    ev_cell = _b2o_make_cell("polygon_news", "news", "AAPL", "2026-01-14", "2026-01-14", "evidence", page_cap=20, item_cap=1000)
    plan = _b2o_make_plan(cells, [ev_cell])
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    evidence_calls = [0]

    async def transport(provider, method, url, **kwargs):
        if kwargs.get("source") == "polygon_news":
            evidence_calls[0] += 1
        raise ConnectionError("market failure")

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=transport))
    assert evidence_calls[0] == 0
    assert report["status"] in ("FAILED", "PARTIAL")
    assert report["stop_reason"] == "market_stage_incomplete"
    conn.close()


def test_market_gate_parent_and_current_lineage(tmp_path: Path):
    from catalyst_data.update_pipeline import execute_update
    cell_a = _b2o_make_cell("polygon_ohlcv", "ohlcv", "AAPL", "2026-01-13", "2026-01-13")
    cell_b = _b2o_make_cell("polygon_ohlcv", "ohlcv", "AAPL", "2026-01-14", "2026-01-14")
    ev_cell = _b2o_make_cell("polygon_news", "news", "AAPL", "2026-01-14", "2026-01-14", "evidence", page_cap=20, item_cap=1000)
    plan = _b2o_make_plan([cell_a, cell_b], [ev_cell])
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)

    run1_count = [0]
    async def t1(provider, method, url, **kwargs):
        run1_count[0] += 1
        if run1_count[0] > 1:
            raise ConnectionError("fail second")
        return _b2o_success_response()

    report1 = asyncio.run(execute_update(db=conn, plan=plan, transport=t1))
    assert report1["status"] in ("FAILED", "PARTIAL")

    async def t2(provider, method, url, **kwargs):
        src = kwargs.get("source", "")
        if src == "polygon_ohlcv":
            return _b2o_success_response()
        return _b2o_news_success()

    report2 = asyncio.run(execute_update(db=conn, plan=plan, transport=t2, parent_run_id=report1["run_id"]))
    assert report2["status"] == "SUCCEEDED"
    conn.close()


# === B. Transport Circuit ===

def test_circuit_opens_after_three_transport_errors(tmp_path: Path):
    from catalyst_data.update_pipeline import execute_update
    cells = _b2o_ohlcv_cells(5)
    plan = _b2o_make_plan(cells)
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)

    async def transport(provider, method, url, **kwargs):
        raise ConnectionError("transport")

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=transport))
    assert report["stop_reason"] == "transport_circuit_open"
    assert conn.execute("SELECT COUNT(*) FROM provider_request_attempts").fetchone()[0] == 3
    conn.close()


def test_circuit_opens_with_exactly_three_cells(tmp_path: Path):
    from catalyst_data.update_pipeline import execute_update
    cells = _b2o_ohlcv_cells(3)
    plan = _b2o_make_plan(cells)
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)

    async def transport(provider, method, url, **kwargs):
        raise ConnectionError("transport")

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=transport))
    assert report["stop_reason"] == "transport_circuit_open"
    conn.close()


def test_circuit_resets_after_success(tmp_path: Path):
    from catalyst_data.update_pipeline import execute_update
    cells = _b2o_ohlcv_cells(6)
    plan = _b2o_make_plan(cells)
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)

    success_at = {2, 5}
    call_idx = [0]
    async def transport(provider, method, url, **kwargs):
        idx = call_idx[0]; call_idx[0] += 1
        if idx in success_at:
            return _b2o_success_response()
        raise ConnectionError("transport")

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=transport))
    assert report.get("stop_reason") != "transport_circuit_open"
    conn.close()


def test_circuit_auth_error_resets_counter(tmp_path: Path):
    from catalyst_data.update_pipeline import execute_update
    cells = _b2o_ohlcv_cells(5)
    plan = _b2o_make_plan(cells)
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)

    call_idx = [0]
    async def transport(provider, method, url, **kwargs):
        idx = call_idx[0]; call_idx[0] += 1
        if idx in (0, 1):
            raise ConnectionError("transport")
        if idx == 2:
            class R: status_code = 403; json = lambda self: {"error": "forbidden"}
            return R()
        raise ConnectionError("transport")

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=transport))
    assert report.get("stop_reason") != "transport_circuit_open"
    conn.close()


def test_circuit_http500_resets_counter(tmp_path: Path):
    from catalyst_data.update_pipeline import execute_update
    cells = _b2o_ohlcv_cells(5)
    plan = _b2o_make_plan(cells)
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)

    call_idx = [0]
    async def transport(provider, method, url, **kwargs):
        idx = call_idx[0]; call_idx[0] += 1
        if idx in (0, 1):
            raise ConnectionError("transport")
        if idx == 2:
            class R: status_code = 503; json = lambda self: {"error": "unavailable"}
            return R()
        raise ConnectionError("transport")

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=transport))
    assert report.get("stop_reason") != "transport_circuit_open"
    conn.close()


# === C. Fatal Transport ===

def test_import_error_is_fatal(tmp_path: Path):
    from catalyst_data.update_pipeline import execute_update, FatalTransportError
    cells = _b2o_ohlcv_cells(3)
    plan = _b2o_make_plan(cells)
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)

    async def transport(provider, method, url, **kwargs):
        raise ImportError("missing dep")

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=transport))
    assert report["status"] == "FAILED"
    assert report["stop_reason"] == "fatal_transport_configuration"
    assert conn.execute("SELECT COUNT(*) FROM provider_request_attempts").fetchone()[0] == 1
    row = conn.execute("SELECT error_class, error_message_redacted FROM provider_request_attempts").fetchone()
    assert row[0] == "fatal_transport_configuration"
    assert "ImportError" not in (row[1] or "")
    conn.close()


def test_fatal_missing_cells_includes_all_incomplete(tmp_path: Path):
    from catalyst_data.update_pipeline import execute_update
    cells = _b2o_ohlcv_cells(3)
    plan = _b2o_make_plan(cells)
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)

    async def transport(provider, method, url, **kwargs):
        raise ImportError("missing dep")

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=transport))
    assert len(report["missing_cells"]) == 3
    for c in cells:
        assert c.cell_id in report["missing_cells"]
    conn.close()


def test_fatal_skips_parent_completed_cells(tmp_path: Path):
    from catalyst_data.update_pipeline import execute_update
    cells = _b2o_ohlcv_cells(3)
    plan = _b2o_make_plan(cells)
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)

    run1_count = [0]
    async def t1(provider, method, url, **kwargs):
        run1_count[0] += 1
        if run1_count[0] > 2:
            raise ConnectionError("fail third")
        return _b2o_success_response()
    report1 = asyncio.run(execute_update(db=conn, plan=plan, transport=t1))

    async def t2(provider, method, url, **kwargs):
        raise ImportError("missing dep")
    report2 = asyncio.run(execute_update(db=conn, plan=plan, transport=t2, parent_run_id=report1["run_id"]))
    assert len(report2["missing_cells"]) == 1
    assert report2["missing_cells"] == [cells[2].cell_id]
    conn.close()


# === D. Persisted Cancellation ===

def test_cancel_via_persisted_flag(tmp_path: Path):
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.ingestion.run_control import request_cancel as db_cancel
    cells = _b2o_ohlcv_cells(3)
    plan = _b2o_make_plan(cells)
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)

    run_id_captured = [None]
    cancel_done = [False]
    async def transport(provider, method, url, **kwargs):
        if not cancel_done[0] and run_id_captured[0]:
            db_cancel(conn, run_id_captured[0])
        cancel_done[0] = True
        return _b2o_success_response()

    import catalyst_data.update_pipeline as up
    orig = up._ensure_b2_run
    def wrapper(*a, **kw):
        run_id_captured[0] = kw.get("run_id") or a[1]
        return orig(*a, **kw)
    up._ensure_b2_run = wrapper
    try:
        report = asyncio.run(execute_update(db=conn, plan=plan, transport=transport))
    finally:
        up._ensure_b2_run = orig

    assert report["status"] == "CANCELLED"
    assert report["stop_reason"] == "operator_cancelled"
    assert conn.execute("SELECT COUNT(*) FROM provider_request_attempts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM provider_request_attempts WHERE status='STARTED'").fetchone()[0] == 0
    conn.close()


# === E. Lineage Resolver ===

def test_lineage_rejects_missing_parent(tmp_path: Path):
    import catalyst_data.coverage_audit as audit
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO ingestion_runs (run_id, plan_hash, expected_plan_hash, ticker_list_json, source_list_json, status, parent_run_id, allow_stale_ohlcv, allow_stale_ohlcv_overridden, cancel_requested, started_at) VALUES ('child', 'abc', 'abc', '[]', '[]', 'PLANNED', 'missing-parent', 0, 0, 0, '2026-01-01T00:00:00Z')")
    conn.commit(); conn.close()
    with pytest.raises(ValueError, match="parent run not found"):
        audit._resolve_b2_lineage(db_path, "child")


def test_lineage_rejects_cycle(tmp_path: Path):
    import catalyst_data.coverage_audit as audit
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO ingestion_runs (run_id, plan_hash, expected_plan_hash, ticker_list_json, source_list_json, status, parent_run_id, allow_stale_ohlcv, allow_stale_ohlcv_overridden, cancel_requested, started_at) VALUES ('a', 'x', 'x', '[]', '[]', 'PLANNED', 'b', 0, 0, 0, '2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO ingestion_runs (run_id, plan_hash, expected_plan_hash, ticker_list_json, source_list_json, status, parent_run_id, allow_stale_ohlcv, allow_stale_ohlcv_overridden, cancel_requested, started_at) VALUES ('b', 'x', 'x', '[]', '[]', 'PLANNED', 'a', 0, 0, 0, '2026-01-01T00:00:00Z')")
    conn.commit(); conn.close()
    with pytest.raises(ValueError, match="cycle"):
        audit._resolve_b2_lineage(db_path, "a")


def test_lineage_rejects_ancestor_plan_drift(tmp_path: Path):
    import catalyst_data.coverage_audit as audit
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO ingestion_runs (run_id, plan_hash, expected_plan_hash, ticker_list_json, source_list_json, status, parent_run_id, allow_stale_ohlcv, allow_stale_ohlcv_overridden, cancel_requested, started_at) VALUES ('gp', 'bad', 'bad', '[]', '[]', 'PLANNED', NULL, 0, 0, 0, '2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO ingestion_runs (run_id, plan_hash, expected_plan_hash, ticker_list_json, source_list_json, status, parent_run_id, allow_stale_ohlcv, allow_stale_ohlcv_overridden, cancel_requested, started_at) VALUES ('p', 'good', 'good', '[]', '[]', 'PLANNED', 'gp', 0, 0, 0, '2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO ingestion_runs (run_id, plan_hash, expected_plan_hash, ticker_list_json, source_list_json, status, parent_run_id, allow_stale_ohlcv, allow_stale_ohlcv_overridden, cancel_requested, started_at) VALUES ('t', 'good', 'good', '[]', '[]', 'PLANNED', 'p', 0, 0, 0, '2026-01-01T00:00:00Z')")
    conn.commit(); conn.close()
    with pytest.raises(ValueError, match="plan_hash drift"):
        audit._resolve_b2_lineage(db_path, "t", plan_hash="good", expected_plan_hash="good")


# === F/G. CLI, Snapshot, Provenance Identity ===

def test_cli_audit_rejects_wrong_terminal_hash(tmp_path: Path):
    from catalyst_data.b2o import main
    db_path = _file_db(tmp_path)
    spec = _spec_path()
    manifest_path = tmp_path / "m.json"
    plan_path = tmp_path / "p.json"
    main(["manifest", "--spec", str(spec), "--output", str(manifest_path), "--latest-complete-session", "2025-08-01"])
    plan_path.write_text(json.dumps({"plan_hash": "correct", "expected_plan_hash": "correct", "config": {"sources": [], "tickers": []}, "universe": {"tickers": []}, "stages": {"market": {"cells": []}, "evidence": {"cells": []}}}))
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO ingestion_runs (run_id, plan_hash, expected_plan_hash, ticker_list_json, source_list_json, status, allow_stale_ohlcv, allow_stale_ohlcv_overridden, cancel_requested, started_at) VALUES ('bad', 'wrong', 'wrong', '[]', '[]', 'PLANNED', 0, 0, 0, '2026-01-01T00:00:00Z')")
    conn.commit(); conn.close()
    rc = main(["audit", "--db", str(db_path), "--universe-manifest", str(manifest_path), "--plan", str(plan_path), "--terminal-run-id", "bad"])
    assert rc != 0


def test_cli_snapshot_rejects_wrong_terminal_hash(tmp_path: Path):
    from catalyst_data.b2o import main
    from catalyst_data.manifests.operations import sha256_file
    db_path = _file_db(tmp_path)
    spec = _spec_path()
    manifest_path = tmp_path / "m.json"
    plan_path = tmp_path / "p.json"
    snap_path = tmp_path / "s.json"
    main(["manifest", "--spec", str(spec), "--output", str(manifest_path), "--latest-complete-session", "2025-08-01"])
    plan_path.write_text(json.dumps({"plan_hash": "correct", "expected_plan_hash": "correct", "config": {"sources": [], "tickers": []}, "universe": {"tickers": []}, "stages": {"market": {"cells": []}, "evidence": {"cells": []}}}))
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO ingestion_runs (run_id, plan_hash, expected_plan_hash, ticker_list_json, source_list_json, status, allow_stale_ohlcv, allow_stale_ohlcv_overridden, cancel_requested, started_at) VALUES ('bad', 'wrong', 'wrong', '[]', '[]', 'PLANNED', 0, 0, 0, '2026-01-01T00:00:00Z')")
    conn.commit(); conn.close()
    rc = main(["snapshot", "--db", str(db_path), "--universe-manifest", str(manifest_path), "--plan", str(plan_path), "--protected-source-sha256", sha256_file(db_path), "--output", str(snap_path), "--terminal-run-id", "bad"])
    assert rc != 0


def test_readiness_ignores_sibling_success(tmp_path: Path):
    import catalyst_data.coverage_audit as audit
    from catalyst_data.manifests.universe import SourceCell
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    cell = SourceCell.create("evidence", "polygon_news", "news", "AAPL", "2025-08-01", "2025-08-01", "calendar_days", "v1", page_cap=20, item_cap=1000)
    conn.execute("INSERT INTO ingestion_runs (run_id, plan_hash, expected_plan_hash, ticker_list_json, source_list_json, status, allow_stale_ohlcv, allow_stale_ohlcv_overridden, cancel_requested, started_at) VALUES ('run-selected', '', '', '[]', '[]', 'PLANNED', 0, 0, 0, '2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO ingestion_runs (run_id, plan_hash, expected_plan_hash, ticker_list_json, source_list_json, status, allow_stale_ohlcv, allow_stale_ohlcv_overridden, cancel_requested, started_at) VALUES ('run-sibling', '', '', '[]', '[]', 'PLANNED', 0, 0, 0, '2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO source_checkpoints (run_id, source_type, ticker, date, status, logical_fetch_id, request_count, pages_received, items_received, is_complete, cell_id, window_start, window_end, endpoint_name, provider_profile_version) VALUES ('run-sibling', 'polygon_news', 'AAPL', '2025-08-01', 'success', ?, 1, 1, 1, 1, ?, '2025-08-01', '2025-08-01', 'news', 'v1')", (cell.cell_id, cell.cell_id))
    conn.commit(); conn.close()
    plan = SimpleNamespace(config={"source_scopes": [{"source_type": "polygon_news", "subjects": ["AAPL"], "date_domain": "calendar_days", "start_date": "2025-08-01", "end_date": "2025-08-01", "stage": "evidence", "request_window_days": 7, "provider_profile_version": "v1", "endpoint_names": ["news"], "page_cap": 20, "item_cap": 1000}]})
    report = audit.run_b2o_readiness_audit(db_path, universe_manifest={"tickers": ["AAPL"]}, plan=plan, terminal_run_id="run-selected")
    gate = report["b2o_readiness"]["canonical_news_comparable_gate"]
    assert gate["status"] == "incomplete"
    assert cell.cell_id in gate["missing_or_incomplete_cell_ids"]


# === H. Resume Regression ===

def test_regression_parent_cells_skipped_in_resume(tmp_path: Path):
    from catalyst_data.update_pipeline import execute_update
    cell_a = _b2o_make_cell("polygon_ohlcv", "ohlcv", "AAPL", "2026-01-13", "2026-01-13")
    cell_b = _b2o_make_cell("polygon_ohlcv", "ohlcv", "AAPL", "2026-01-14", "2026-01-14")
    plan = _b2o_make_plan([cell_a, cell_b])
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)

    r1 = [0]
    async def t1(provider, method, url, **kwargs):
        r1[0] += 1
        if r1[0] > 1: raise ConnectionError("fail")
        return _b2o_success_response()
    report1 = asyncio.run(execute_update(db=conn, plan=plan, transport=t1))

    processed = []
    async def t2(provider, method, url, **kwargs):
        processed.append(kwargs.get("window_start", ""))
        return _b2o_success_response()
    report2 = asyncio.run(execute_update(db=conn, plan=plan, transport=t2, parent_run_id=report1["run_id"]))
    assert report2["cells_skipped"] >= 1
    assert "2026-01-13" not in processed
    assert "2026-01-14" in processed
    conn.close()


# === I. AST Report Key Uniqueness (already appended) ===
# test_report_dict_has_no_duplicate_keys_ast is above
# test_readiness_rejects_missing_terminal_run_id is above
# test_readiness_rejects_empty_terminal_run_id is above

# === B2-O-X: Transient transport retry tests (Task 1) ===

def test_transient_connecterror_recovers_on_third_attempt(tmp_path: Path):
    """Two ConnectErrors then success → 3 attempts, cell success."""
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)

    cell = SourceCell.create(
        "evidence", "polygon_news", "news", "AAPL",
        "2025-08-01", "2025-08-01", "calendar_days", "v1",
        page_cap=1, item_cap=10,
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    delays = []

    class RetryTransport:
        def __init__(self):
            self.calls = 0

        async def retry_sleep(self, delay):
            delays.append(delay)

        async def request(self, **kwargs):
            self.calls += 1
            if self.calls <= 2:
                raise ConnectionError("transient connect failure")
            return {
                "status": 200,
                "body": json.dumps({
                    "results": [{"id": "art1", "title": "OK",
                                 "published_utc": "2025-08-01T12:00:00Z",
                                 "description": "d",
                                 "article_url": "https://x.com/1",
                                 "tickers": ["AAPL"],
                                 "publisher": {"name": "Test"}}],
                }).encode(),
            }

    import asyncio
    report = asyncio.run(execute_update(db=conn, plan=plan, transport=RetryTransport()))

    assert report["status"] == "SUCCEEDED"

    # Ledger: attempt_no 1 and 2 TRANSPORT_ERROR, attempt 3 SUCCEEDED
    attempts = conn.execute(
        "SELECT attempt_no, status FROM provider_request_attempts ORDER BY attempt_no"
    ).fetchall()
    assert attempts == [(1, "TRANSPORT_ERROR"), (2, "TRANSPORT_ERROR"), (3, "SUCCEEDED")], f"unexpected: {attempts}"

    # Same logical_fetch_id
    lfs = conn.execute(
        "SELECT DISTINCT logical_fetch_id FROM provider_request_attempts"
    ).fetchall()
    assert len(lfs) == 1, f"multiple logical_fetch_ids: {lfs}"

    # Checkpoint
    cp = conn.execute(
        "SELECT status, error_class, is_complete, request_count, pages_received, items_received FROM source_checkpoints WHERE cell_id=?",
        (cell.cell_id,),
    ).fetchone()
    assert cp == ("success", None, 1, 3, 1, 1), f"unexpected checkpoint: {cp}"

    # Delays: approx 5s then 10s
    assert len(delays) == 2, f"expected 2 delays, got {len(delays)}"
    assert 4.5 <= delays[0] <= 5.5, f"delay[0]={delays[0]}"
    assert 9.5 <= delays[1] <= 10.5, f"delay[1]={delays[1]}"

    conn.close()


def test_transient_connecterror_exhaustion_three_failures(tmp_path: Path):
    """Three ConnectErrors → cell failed, is_complete=0, error_class=transport_error."""
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cell = SourceCell.create(
        "evidence", "polygon_news", "news", "AAPL",
        "2025-08-01", "2025-08-01", "calendar_days", "v1",
        page_cap=1, item_cap=10,
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    delays = []

    class ExhaustTransport:
        def __init__(self):
            self.calls = 0

        async def retry_sleep(self, delay):
            delays.append(delay)

        async def request(self, **kwargs):
            self.calls += 1
            raise ConnectionError("always down")

    import asyncio
    report = asyncio.run(execute_update(db=conn, plan=plan, transport=ExhaustTransport()))

    # 3 attempts, all TRANSPORT_ERROR
    attempts = conn.execute(
        "SELECT attempt_no, status FROM provider_request_attempts ORDER BY attempt_no"
    ).fetchall()
    assert [(r[0], r[1]) for r in attempts] == [(1, "TRANSPORT_ERROR"), (2, "TRANSPORT_ERROR"), (3, "TRANSPORT_ERROR")], f"unexpected: {[(r[0], r[1]) for r in attempts]}"

    # Checkpoint: failed, error_class=transport_error, no pages
    cp = conn.execute(
        "SELECT status, error_class, is_complete, request_count, pages_received, items_received FROM source_checkpoints WHERE cell_id=?",
        (cell.cell_id,),
    ).fetchone()
    assert cp["status"] == "failed", f"expected failed, got {cp['status']}"
    assert cp["error_class"] == "transport_error", f"expected transport_error, got {cp['error_class']}"
    assert cp["is_complete"] == 0
    assert cp["request_count"] == 3
    assert cp["pages_received"] == 0
    assert cp["items_received"] == 0

    # No raw_assets, no articles, no provenance
    assert conn.execute("SELECT COUNT(*) FROM raw_assets WHERE request_id IS NOT NULL").fetchone()[0] == 0

    # Two delays: ~5s, ~10s
    assert len(delays) == 2
    assert 4.5 <= delays[0] <= 5.5
    assert 9.5 <= delays[1] <= 10.5

    conn.close()


def test_circuit_opens_after_three_exhausted_cells(tmp_path: Path):
    """Three cells each exhaust 3 ConnectErrors → 9 attempts, circuit opens."""
    from catalyst_data.update_pipeline import execute_update
    cells = _b2o_ohlcv_cells(4)
    plan = _b2o_make_plan(cells)
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)

    delays = []

    async def transport(provider, method, url, **kwargs):
        if hasattr(transport, "retry_sleep_fn"):
            pass  # handled by _request_b2_page internally
        raise ConnectionError("transport")

    # We need to provide retry_sleep to let retries proceed
    class TransRetryTransport:
        async def retry_sleep(self, delay):
            delays.append(delay)

        async def request(self, provider=None, method=None, url=None, **kwargs):
            raise ConnectionError("transport")

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=TransRetryTransport()))

    # Circuit should open after 3 cells × 3 attempts = 9 total
    total_attempts = conn.execute("SELECT COUNT(*) FROM provider_request_attempts").fetchone()[0]
    assert total_attempts == 9, f"expected 9 attempts, got {total_attempts}"
    assert report["stop_reason"] == "transport_circuit_open"
    conn.close()


def test_circuit_resets_after_transient_recovery(tmp_path: Path):
    """Cell recovers after ConnectErrors → counter resets."""
    from catalyst_data.update_pipeline import execute_update
    cells = _b2o_ohlcv_cells(3)
    plan = _b2o_make_plan(cells)
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)

    delays = []

    class RecoveryTransport:
        def __init__(self):
            self.cell_calls = 0

        async def retry_sleep(self, delay):
            delays.append(delay)

        async def request(self, **kwargs):
            self.cell_calls += 1
            # First cell: fail
            if self.cell_calls == 1:
                raise ConnectionError("down")
            # Second cell: succeed immediately
            return _FetchResult_200()

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=RecoveryTransport()))
    # Circuit should NOT open — second cell succeeded
    assert report.get("stop_reason") != "transport_circuit_open"
    conn.close()


def _FetchResult_200():
    from types import SimpleNamespace
    return SimpleNamespace(status=200, headers={},
        text='{"results":[],"status":"OK"}',
        content=b'{"results":[],"status":"OK"}',
        json=lambda: {"results": [], "status": "OK"})

# === Finding 1: Independent retry budgets ===

def test_mixed_transport_and_429_have_independent_budgets(tmp_path: Path):
    """Transport errors and 429 must not consume each other's retry budgets."""
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cell = SourceCell.create(
        "evidence", "polygon_news", "news", "AAPL",
        "2025-08-01", "2025-08-01", "calendar_days", "v1",
        page_cap=1, item_cap=10,
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    delays = []
    seq = [0]

    class MixedTransport:
        async def retry_sleep(self, delay):
            delays.append(delay)

        async def request(self, **kwargs):
            seq[0] += 1
            if seq[0] in (1, 2):
                raise ConnectionError("down")
            if seq[0] in (3, 4):
                return {"status": 429, "body": b'{"error":"rate limited"}',
                        "headers": {"retry-after": "1"}}
            return {"status": 200, "body": json.dumps({
                "results": [{"id": "a1", "title": "OK", "published_utc": "2025-08-01T12:00:00Z",
                             "description": "d", "article_url": "https://x.com/1",
                             "tickers": ["AAPL"], "publisher": {"name": "T"}}],
            }).encode()}

    import asyncio
    report = asyncio.run(execute_update(db=conn, plan=plan, transport=MixedTransport()))
    assert report["status"] == "SUCCEEDED"

    attempts = conn.execute(
        "SELECT attempt_no, status FROM provider_request_attempts ORDER BY attempt_no"
    ).fetchall()
    expected = [(1, "TRANSPORT_ERROR"), (2, "TRANSPORT_ERROR"), (3, "RATE_LIMITED"), (4, "RATE_LIMITED"), (5, "SUCCEEDED")]
    assert [(r[0], r[1]) for r in attempts] == expected, f"unexpected: {[(r[0], r[1]) for r in attempts]}"

    cp = conn.execute(
        "SELECT status, is_complete, request_count, items_received FROM source_checkpoints WHERE cell_id=?",
        (cell.cell_id,),
    ).fetchone()
    assert cp["is_complete"] == 1
    conn.close()


def test_mixed_transport_and_503_have_independent_budgets(tmp_path: Path):
    """Transport errors must not consume Polygon's five-attempt 5xx budget."""
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)

    cell = SourceCell.create(
        "evidence", "polygon_news", "news", "AAPL",
        "2025-08-01", "2025-08-01", "calendar_days", "v1",
        page_cap=1, item_cap=10,
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    call_count = 0

    class MixedTransport:
        async def retry_sleep(self, delay):
            return None

        async def request(self, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ConnectionError("down")
            if call_count <= 6:
                return {"status": 503, "body": b'{"error":"unavailable"}'}
            return {"status": 200, "body": b'{"results":[],"status":"OK"}'}

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=MixedTransport()))

    assert report["status"] == "SUCCEEDED"
    assert conn.execute(
        "SELECT attempt_no, status FROM provider_request_attempts ORDER BY attempt_no"
    ).fetchall() == [
        (1, "TRANSPORT_ERROR"),
        (2, "TRANSPORT_ERROR"),
        (3, "HTTP_ERROR"),
        (4, "HTTP_ERROR"),
        (5, "HTTP_ERROR"),
        (6, "HTTP_ERROR"),
        (7, "SUCCEEDED"),
    ]
    conn.close()


# === Finding 2: Specific transient exceptions only ===

def test_non_transient_exception_is_fatal_not_retried(tmp_path: Path):
    """ValueError/KeyError must stop immediately, not wait 5/10s or count toward circuit."""
    from catalyst_data.update_pipeline import execute_update

    cells = _b2o_ohlcv_cells(1)
    plan = _b2o_make_plan(cells)
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    delays = []

    class BadTransport:
        async def retry_sleep(self, delay):
            delays.append(delay)

        async def request(self, **kwargs):
            raise ValueError("bad input, not transient")

    import asyncio
    report = asyncio.run(execute_update(db=conn, plan=plan, transport=BadTransport()))

    # Must be fatal — no retry delay, no circuit counter increment
    assert len(delays) == 0, f"expected 0 retry sleep calls, got {len(delays)}"
    assert report["stop_reason"] == "fatal_transport_configuration"

    # Single attempt, not 3
    attempts = conn.execute("SELECT COUNT(*) FROM provider_request_attempts").fetchone()[0]
    assert attempts == 1, f"expected 1 attempt, got {attempts}"

    conn.close()


def test_real_httpx_connecterror_is_retried(monkeypatch, tmp_path: Path):
    """Real httpx.ConnectError must trigger retry (not bare ConnectionError)."""
    import httpx
    from catalyst_data.update_pipeline import execute_update

    cells = _b2o_ohlcv_cells(1)
    plan = _b2o_make_plan(cells)
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    delays = []
    call_count = [0]

    class HttpxTransport:
        async def retry_sleep(self, delay):
            delays.append(delay)

        async def request(self, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise httpx.ConnectError("real connect failure")
            return _FetchResult_200()

    import asyncio
    report = asyncio.run(execute_update(db=conn, plan=plan, transport=HttpxTransport()))

    # Must have retried twice (5s, 10s)
    assert len(delays) == 2, f"expected 2 retry delays, got {len(delays)}"
    assert call_count[0] == 3
    attempts = conn.execute("SELECT COUNT(*) FROM provider_request_attempts").fetchone()[0]
    assert attempts == 3
    conn.close()


# === Finding 3: Limiter integration ===

def test_limiter_acquired_per_attempt_during_retry(tmp_path: Path):
    """Each retry attempt must pass through the rate limiter (via execute_update pipeline)."""
    from catalyst_data.b2o import B2OLiveTransport, ProviderCredentials
    from catalyst_data.config import RatePolicy
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash
    import httpx
    import asyncio
    from dataclasses import asdict

    limiter_acquires = [0]

    class FakeLimiter:
        def acquire(self):
            limiter_acquires[0] += 1
            from contextlib import asynccontextmanager
            @asynccontextmanager
            async def ctx():
                yield
            return ctx()

    handler_calls = [0]

    async def handler(request):
        handler_calls[0] += 1
        if handler_calls[0] <= 2:
            raise httpx.ConnectError("fake")
        return httpx.Response(200, json={"results": [], "status": "OK"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    b2o_transport = B2OLiveTransport(
        credentials=ProviderCredentials(polygon="test-key"),
        rate_policies={"polygon": RatePolicy(0, 1, None)},
        request_caps={},
        clients={"polygon": client},
    )
    b2o_transport._limiters["polygon"] = FakeLimiter()

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cell = SourceCell.create(
        "evidence", "polygon_news", "news", "AAPL",
        "2025-08-01", "2025-08-01", "calendar_days", "v1",
        page_cap=1, item_cap=10,
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    report = asyncio.run(execute_update(db=conn, plan=plan, transport=b2o_transport))
    asyncio.run(client.aclose())
    assert report["status"] == "SUCCEEDED"
    assert handler_calls[0] == 3, f"expected 3 HTTP handler calls, got {handler_calls[0]}"
    assert limiter_acquires[0] == 3, f"expected 3 limiter acquires, got {limiter_acquires[0]}"
    assert conn.execute("SELECT COUNT(*) FROM provider_request_attempts").fetchone()[0] == 3
    conn.close()


# === Finding 4: Fixed circuit reset test ===

def test_retry_circuit_resets_after_successful_retry(tmp_path: Path):
    """Cell A exhausts → counter=1. Cell B retries and succeeds → reset. C+D exhaust → counter=2, E still runs."""
    from catalyst_data.update_pipeline import execute_update

    cells = _b2o_ohlcv_cells(5)
    plan = _b2o_make_plan(cells)
    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    delays = []
    cell_calls = [0]

    class ResetTransport:
        async def retry_sleep(self, delay):
            delays.append(delay)

        async def request(self, **kwargs):
            cell_calls[0] += 1
            # Cell indices: 1-3 = cell A, 4-6 = cell B, 7-9 = cell C, 10-12 = cell D, 13+ = cell E
            cell_idx = (cell_calls[0] - 1) // 3 + 1
            call_in_cell = (cell_calls[0] - 1) % 3 + 1

            if cell_idx == 1:  # Cell A: always fail (exhaust)
                raise ConnectionError("down")
            elif cell_idx == 2:  # Cell B: fail first two, succeed on third
                if call_in_cell <= 2:
                    raise ConnectionError("down")
                return _FetchResult_200()
            elif cell_idx in (3, 4):  # Cells C, D: always fail
                raise ConnectionError("down")
            else:  # Cell E: must still run
                return _FetchResult_200()

    import asyncio
    report = asyncio.run(execute_update(db=conn, plan=plan, transport=ResetTransport()))

    # Cell E must have run (no premature circuit open)
    assert report.get("stop_reason") != "transport_circuit_open"

    # Verify per-cell attempts
    cells_cp = conn.execute(
        "SELECT cell_id, request_count, pages_received, is_complete FROM source_checkpoints ORDER BY date"
    ).fetchall()
    assert len(cells_cp) == 5
    # Cell A: 3 attempts, failed
    assert cells_cp[0]["request_count"] == 3
    assert cells_cp[0]["is_complete"] == 0
    # Cell B: 3 attempts, succeeded
    assert cells_cp[1]["request_count"] == 3
    assert cells_cp[1]["is_complete"] == 1
    # Cell C: 3 attempts, failed
    assert cells_cp[2]["request_count"] == 3
    assert cells_cp[2]["is_complete"] == 0
    # Cell D: 3 attempts, failed
    assert cells_cp[3]["request_count"] == 3
    assert cells_cp[3]["is_complete"] == 0
    # Cell E: 1 attempt, succeeded
    assert cells_cp[4]["request_count"] == 1
    assert cells_cp[4]["is_complete"] == 1

    conn.close()


# === Finding 5: Projection semantics ===

def test_page1_transport_exhaustion_zero_projections(tmp_path: Path):
    """Page 1 transport exhaustion → zero raw_assets, zero articles, zero provenance."""
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cell = SourceCell.create(
        "evidence", "polygon_news", "news", "AAPL",
        "2025-08-01", "2025-08-01", "calendar_days", "v1",
        page_cap=1, item_cap=10,
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    class ExhaustTransport:
        async def retry_sleep(self, delay): pass
        async def request(self, **kwargs):
            raise ConnectionError("always down")

    import asyncio
    asyncio.run(execute_update(db=conn, plan=plan, transport=ExhaustTransport()))

    assert conn.execute("SELECT COUNT(*) FROM raw_assets WHERE request_id IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM normalized_provenance").fetchone()[0] == 0
    cp = conn.execute("SELECT status, is_complete, request_count FROM source_checkpoints WHERE cell_id=?",
                      (cell.cell_id,)).fetchone()
    assert cp["status"] == "failed"
    assert cp["is_complete"] == 0
    assert cp["request_count"] == 3
    conn.close()


def test_page1_success_page2_exhaustion_preserves_page1(tmp_path: Path):
    """Page 1 success + page 2 transport exhaustion → page 1 data preserved, cell failed."""
    from catalyst_data.manifests.universe import SourceCell
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    db_path = _file_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cell = SourceCell.create(
        "evidence", "polygon_news", "news", "AAPL",
        "2025-08-01", "2025-08-07", "calendar_days", "v1",
        page_cap=2, item_cap=10,
    )
    plan = UpdatePlan(
        config={"source_scopes": {}, "sources": ["polygon_news"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [asdict(cell)]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    page_calls = [0]

    class TwoPageTransport:
        async def retry_sleep(self, delay): pass

        async def request(self, **kwargs):
            page_calls[0] += 1
            pu = kwargs.get("page_url")
            if pu is None:
                # Page 1: success with next_url
                return {"status": 200, "body": json.dumps({
                    "results": [{"id": "p1", "title": "OK", "published_utc": "2025-08-01T12:00:00Z",
                                 "description": "d", "article_url": "https://x.com/1",
                                 "tickers": ["AAPL"], "publisher": {"name": "T"}}],
                    "next_url": "https://api.polygon.io/v2/reference/news?cursor=next",
                }).encode()}
            else:
                # Page 2: transport error (exhausts all 3 attempts on each call)
                # The retry mechanism will call this 3 times for page 2
                raise ConnectionError("page 2 down")

    import asyncio
    report = asyncio.run(execute_update(db=conn, plan=plan, transport=TwoPageTransport()))

    # Page 1 article and provenance must exist
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1, "page 1 article lost"
    assert conn.execute("SELECT COUNT(*) FROM normalized_provenance").fetchone()[0] == 1, "page 1 provenance lost"
    assert conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0] == 1
    attempts_by_page = conn.execute(
        "SELECT page_no, COUNT(*) FROM provider_request_attempts GROUP BY page_no ORDER BY page_no"
    ).fetchall()
    assert [tuple(row) for row in attempts_by_page] == [(1, 1), (2, 3)]

    # Checkpoint: failed, is_complete=0
    cp = conn.execute("SELECT status, error_class, is_complete, request_count, pages_received, items_received FROM source_checkpoints WHERE cell_id=?",
                      (cell.cell_id,)).fetchone()
    assert cp["status"] == "failed"
    assert cp["is_complete"] == 0
    assert cp["pages_received"] == 1, f"expected pages_received=1, got {cp['pages_received']}"
    assert cp["items_received"] == 1

    conn.close()
