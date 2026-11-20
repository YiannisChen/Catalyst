"""The source registry, the systemd units in deploy/ and the monitor's checks agree (one source of truth)."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

from catalyst_data.ingestion import sources
from catalyst_data.ingestion.ops import health

REPO = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("monitor", REPO / "deploy/monitor/monitor.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def test_registry_matches_the_systemd_units():
    assert sources.problems() == []


def test_unit_paths_include_ops_units(tmp_path):
    ops = tmp_path / "ops"
    ops.mkdir()
    unit = ops / "catalyst-health.timer"
    unit.write_text("[Unit]\nDescription=health\n")
    assert sources.unit_paths(tmp_path, "catalyst-*.timer") == [unit]


def test_every_registry_store_is_in_the_health_report_and_nothing_else():
    assert set(sources.load()["stores"]) == set(health.KEYS)


def test_the_monitor_checks_exactly_the_active_registry_sources():
    reg = sources.load()
    at = datetime(2026, 12, 1, 15, tzinfo=timezone.utc)  # long after every ``since``
    report = {"generated_at": "2026-12-01T14:55:00Z", "sources": [], "registry": reg, "units": {"failed": []},
              "daily_job": {"created_at": "2026-12-01T11:00:00Z", "status": "OK"}, "disk": {"free_pct": 80},
              "backup": {"latest": "2026-12-01", "manifest_at": "2026-12-01T09:00:00Z"}}
    checked = {k.split(":", 1)[1] for k in M.problems(report, None, at) if k.startswith("never_ran:")}
    expected = {s["id"] for s in reg["sources"] if s["status"] in sources.ACTIVE and s["expected_hours"] is not None}
    assert checked == expected
    assert not hasattr(M, "EXPECTED_HOURS") and not hasattr(M, "BEST_EFFORT")  # no second list of sources


def test_disabled_sources_are_not_run():
    for s in sources.load()["sources"]:
        if s["status"] not in sources.ACTIVE:
            assert not sources.active(s["id"]) and not s["timers"], s["id"]
    assert sources.unit_sources() == {}
    assert "catalyst-collect-alpaca-news" in sources.timers()


def test_problems_catch_drift(tmp_path):
    for f in sources.unit_paths(sources.DEPLOY, "catalyst-*.timer"):
        (tmp_path / f.name).write_text(f.read_text())
    for f in sources.unit_paths(sources.DEPLOY, "catalyst-collect-*.service"):
        (tmp_path / f.name).write_text(f.read_text())
    (tmp_path / "catalyst-collect-orphan.timer").write_text("[Timer]\n")
    reg = json.loads(sources.REGISTRY.read_text())
    reg["sources"].append({**reg["sources"][0], "id": "x:y", "status": "trial", "timers": ["catalyst-collect-nowhere"]})
    found = sources.problems(reg, tmp_path)
    assert any("catalyst-collect-orphan" in p for p in found)
    assert any("catalyst-collect-nowhere" in p for p in found)


def test_every_active_news_source_has_an_independent_time_reference():
    for s in sources.load()["sources"]:
        if s["status"] in sources.ACTIVE and s["category"] in ("news", "press_release", "sec"):
            assert s.get("time_reference"), s["id"]


def test_daily_price_has_its_own_market_store_and_source():
    reg = sources.load()
    registered = {row["id"]: row for row in reg["sources"]}
    assert Path(reg["stores"]["market"]).name == "market_context.sqlite3"
    assert registered["daily:price_bars"]["store"] == "market"
    assert health._store_path("market", reg["stores"]["market"]) == health.DATA / "market_context.sqlite3"
