"""Synthetic immutable snapshots; no production or evaluation data is read."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def module():
    path = Path(__file__).resolve().parents[3] / "deploy/ops/shadow_compare.py"
    spec = importlib.util.spec_from_file_location("shadow_compare", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def case(tmp_path):
    source = "alpaca_news:news:poll"
    registry = {"sources": [{"id": source, "store": "alpaca_news", "key": "news:poll",
                             "status": "trial", "category": "news", "expected_hours": 1,
                             "timers": ["catalyst-collect-alpaca-news"]}]}
    plan = {"schema_version": 1, "timestamp_policy": "stored_utc_timefields_v1", "sources": [{"id": source,
            "production_cadence_seconds": 3600, "shadow_cadence_seconds": 3600}],
            "revisions": {"production": "a" * 40, "shadow": "a" * 40},
            "snapshots": {}, "snapshot_captured_at": {"production": "2026-10-24T00:10:00Z",
                                                       "shadow": "2026-10-24T00:10:00Z"}}
    runs = {"schema_version": 1, "runs": []}
    for side in ("production", "shadow"):
        path = tmp_path / (side + ".sqlite3")
        with sqlite3.connect(path) as c:
            c.executescript("""CREATE TABLE news (id INTEGER PRIMARY KEY, created_at TEXT,
                updated_at TEXT, headline TEXT, content TEXT, url TEXT, first_seen_at TEXT);
                CREATE TABLE intake_log (subject TEXT, attempted_at TEXT, status TEXT);""")
            c.execute("INSERT INTO news VALUES (1, ?, ?, 'title', 'body', 'https://example.com/a', ?)",
                      ("2026-10-23T02:00:00Z", "2026-10-23T02:00:00Z", "2026-10-23T02:05:00Z"))
            for hour in range(24):
                stamp = f"2026-10-23T{hour:02}:00:00Z"
                c.execute("INSERT INTO intake_log VALUES ('news:poll', ?, 'OK')", (stamp,))
                runs["runs"].append({"side": side, "source_id": source, "started_at": stamp,
                                     "finished_at": stamp, "status": "OK", "revision": "a" * 40,
                                     "cadence_seconds": 3600})
        plan["snapshots"][side] = {"alpaca_news": str(path)}
    return registry, plan, runs


def compare(module, case):
    registry, plan, runs = case
    return module.compare(registry, plan, runs, start="2026-10-23T00:00:00Z",
                          end="2026-10-24T00:00:00Z", now="2026-10-24T01:00:00Z")


def test_equal_complete_snapshots_are_ready(module, case):
    result = compare(module, case)
    assert result["scope_ready"] is True
    assert result["news_comparison_ready"] is True
    assert result["deployment_approval"] == "not_assessed_requires_owner_confirmation"
    assert "switch_ready" not in result
    assert result["sources"]["alpaca_news:news:poll"]["matched_items"] == 1


@pytest.mark.parametrize("field,value", [("headline", "changed"), ("content", "changed"),
                                          ("created_at", "2026-10-23T03:00:00Z")])
def test_key_field_changes_fail(module, case, field, value):
    with sqlite3.connect(case[1]["snapshots"]["shadow"]["alpaca_news"]) as c:
        c.execute(f"UPDATE news SET {field} = ?", (value,))
    result = compare(module, case)
    assert not result["scope_ready"]
    assert result["sources"]["alpaca_news:news:poll"]["field_mismatches"]


def test_missing_source_items_do_not_pass(module, case):
    with sqlite3.connect(case[1]["snapshots"]["shadow"]["alpaca_news"]) as c:
        c.execute("DELETE FROM news")
    assert not compare(module, case)["scope_ready"]


def test_missing_ledger_does_not_pass(module, case):
    with sqlite3.connect(case[1]["snapshots"]["production"]["alpaca_news"]) as c:
        c.execute("DELETE FROM intake_log")
    assert not compare(module, case)["scope_ready"]


def test_missing_snapshot_does_not_create_file(module, case):
    path = Path(case[1]["snapshots"]["shadow"]["alpaca_news"])
    path.unlink()
    assert not compare(module, case)["scope_ready"]
    assert not path.exists()


def test_runs_must_cover_full_day_and_both_sides(module, case):
    case[2]["runs"] = [r for r in case[2]["runs"] if r["side"] == "production"]
    assert not compare(module, case)["scope_ready"]


def test_legacy_timestamp_runs_are_unverifiable(module, case):
    case = (case[0], case[1], "2026-10-23T00:00:00Z\n")
    with pytest.raises(ValueError, match="runs"):
        compare(module, case)


def test_short_window_rejected(module, case):
    with pytest.raises(ValueError, match="24"):
        module.compare(*case, start="2026-10-23T00:00:00Z", end="2026-10-23T23:00:00Z",
                       now="2026-10-24T01:00:00Z")


def test_naive_window_rejected(module, case):
    with pytest.raises(ValueError, match="timezone|UTC"):
        module.compare(*case, start="2026-10-23T00:00:00", end="2026-10-24T00:00:00Z",
                       now="2026-10-24T01:00:00Z")


def test_cadence_mismatch_requires_restart(module, case):
    case[1]["sources"][0]["shadow_cadence_seconds"] = 7200
    result = compare(module, case)
    assert not result["scope_ready"]
    assert "restart" in json.dumps(result).lower()


def test_missing_expected_news_source_still_blocks_news_gate(module, case):
    case[0]["sources"].append({"id": "future_news:wire", "store": "future_news", "key": "wire",
                               "category": "news", "status": "trial", "expected_hours": 1,
                               "timers": ["collect-future"]})
    result = compare(module, case)
    assert result["scope_ready"]
    assert not result["news_comparison_ready"]
    assert result["uncovered_news_sources"] == ["future_news:wire"]


def test_archive_cannot_be_selected_as_news_adapter(module, case):
    source = {"id": "regulatory:openfda", "store": "regulatory", "key": "openfda",
              "category": "regulatory", "status": "enabled", "expected_hours": 24, "timers": ["collect-fda"]}
    case[0]["sources"].append(source)
    case[1]["sources"].append({"id": source["id"], "production_cadence_seconds": 86400,
                               "shadow_cadence_seconds": 86400})
    result = compare(module, case)
    assert not result["scope_ready"]
    assert not result["news_comparison_ready"]


def test_item_outside_common_window_is_excluded(module, case):
    with sqlite3.connect(case[1]["snapshots"]["production"]["alpaca_news"]) as c:
        c.execute("INSERT INTO news VALUES (2, '2026-10-22T00:00:00Z', NULL, 'old', 'old',"
                  " 'https://example.com/old', '2026-10-22T00:01:00Z')")
    assert compare(module, case)["scope_ready"]


def test_cli_requires_manifest_before_opening_inputs(module, monkeypatch, capsys):
    monkeypatch.delenv("CATALYST_INPUT_MANIFEST", raising=False)
    assert module.main(["--registry", "/missing/registry.json", "--plan", "/missing/plan.json",
                        "--runs", "/missing/runs.json", "--start", "2026-10-23T00:00:00Z",
                        "--end", "2026-10-24T00:00:00Z"]) == 2
    assert "CATALYST_INPUT_MANIFEST" in capsys.readouterr().out


def test_unrecorded_poll_gap_fails(module, case):
    case[2]["runs"] = [r for r in case[2]["runs"]
                        if not (r["side"] == "shadow" and r["started_at"] == "2026-10-23T12:00:00Z")]
    assert not compare(module, case)["scope_ready"]


def test_midwindow_revision_change_fails(module, case):
    case[2]["runs"][1]["revision"] = "b" * 40
    assert not compare(module, case)["scope_ready"]


def test_ledger_error_cannot_be_hidden_by_equal_items(module, case):
    with sqlite3.connect(case[1]["snapshots"]["shadow"]["alpaca_news"]) as c:
        c.execute("UPDATE intake_log SET status = 'ERROR' WHERE attempted_at = '2026-10-23T12:00:00Z'")
    assert not compare(module, case)["scope_ready"]


def test_canonical_url_drops_tracking_and_fragment_only(module):
    assert module.canonical_url("https://EXAMPLE.com/a?utm_source=x&article=2#top") == "https://example.com/a?article=2"
    assert module.canonical_url("https://example.com/a?article=3") != "https://example.com/a?article=2"


def test_manifest_receipt_precedes_comparison_and_pins_snapshots(module, case, tmp_path, monkeypatch, capsys):
    import hashlib

    from catalyst_data.ingestion import input_manifest

    paths = []
    for name, value in zip(("registry", "plan", "runs"), case):
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(value))
        paths.append(path)
    argv = ["--registry", str(paths[0]), "--plan", str(paths[1]), "--runs", str(paths[2]),
            "--start", "2026-10-23T00:00:00Z", "--end", "2026-10-24T00:00:00Z"]
    files = [*paths, Path(module.__file__).resolve(), Path(input_manifest.__file__).resolve(), Path(module.timefields.__file__).resolve()]
    files += [Path(stores["alpaca_news"]) for stores in case[1]["snapshots"].values()]
    names = ("PYTHONPATH", "CATALYST_ARCHIVE_DIR", "CATALYST_DATA_DIR", "CATALYST_RATELIMIT_DIR", "CATALYST_RESILIENCE_DB")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    spec = {"schema_version": 1, "invocation": [str(Path(module.__file__).resolve()), *argv],
            "environment": {name: None for name in names},
            "files": [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(spec))
    monkeypatch.setenv("CATALYST_INPUT_MANIFEST", str(manifest))
    monkeypatch.setenv("CATALYST_INPUT_RECEIPT", str(tmp_path / "receipt.json"))
    module.main(argv)  # The real wall clock may precede the synthetic window end.
    first = json.loads(capsys.readouterr().out.splitlines()[0])
    assert "input_manifest" in first
    pinned = {f["path"] for f in first["input_manifest"]["files"]}
    assert {str(p) for p in files}.issubset(pinned)



def test_preexisting_production_inventory_uses_common_publication_window(module, case):
    with sqlite3.connect(case[1]["snapshots"]["production"]["alpaca_news"]) as c:
        c.execute("UPDATE news SET first_seen_at = '2026-10-22T23:59:00Z'")
    assert compare(module, case)["scope_ready"]


def test_old_publication_updates_are_explicitly_outside_scope(module, case):
    for side in ("production", "shadow"):
        with sqlite3.connect(case[1]["snapshots"][side]["alpaca_news"]) as c:
            c.execute("INSERT INTO news VALUES (2, '2026-10-22T00:00:00Z', '2026-10-23T10:00:00Z', 'old', ?,"
                      " 'https://example.com/old', '2026-10-23T10:01:00Z')", (side,))
    result = compare(module, case)
    assert result["scope_ready"]
    assert "older-publication" in " ".join(result["limitations"])


def test_undated_observed_row_blocks_acceptance(module, case):
    with sqlite3.connect(case[1]["snapshots"]["shadow"]["alpaca_news"]) as c:
        c.execute("UPDATE news SET created_at = NULL")
    assert not compare(module, case)["scope_ready"]


def test_daily_job_is_separate_from_collectors_global_scope(module, case):
    case[0]["sources"].append({"id": "daily:sec", "store": "daily", "key": "sec",
                               "status": "enabled", "expected_hours": None, "timers": ["catalyst-daily"]})
    assert compare(module, case)["news_comparison_ready"]


def test_missing_capture_provenance_fails(module, case):
    case[1]["snapshot_captured_at"]["shadow"] = "2026-10-23T23:59:00Z"
    with pytest.raises(ValueError, match="snapshot capture"):
        compare(module, case)


def test_uniform_wrong_run_revision_cannot_match_pinned_plan(module, case):
    case[1]["revisions"] = {"production": "a" * 40, "shadow": "a" * 40}
    for run in case[2]["runs"]:
        if run["side"] == "shadow":
            run["revision"] = "b" * 40
    assert not compare(module, case)["scope_ready"]
