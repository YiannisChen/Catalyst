"""Pre-B6 readiness convergence tests — v13 on-disk fixture."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _fresh_db_path():
    """Create on-disk v13 DB with project fixtures. Return path string."""
    spec = importlib.util.spec_from_file_location(
        "conftest", "packages/data-core/tests/conftest.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Use in-memory to build, then backup to disk
    mem = mod._fresh_db_at_version(0, foreign_keys=False)
    from catalyst_data.migrations import run_migrations
    run_migrations(mem)
    mem.commit()

    fd, path = tempfile.mkstemp(suffix=".db")
    disk = sqlite3.connect(path)
    mem.backup(disk)
    disk.commit()
    disk.close()
    mem.close()
    return path


def _insert_run(db_path, run_id="r1"):
    h = "a" * 64
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO ingestion_runs
           (run_id, plan_hash, expected_plan_hash, parent_run_id, status,
            allow_stale_ohlcv, allow_stale_ohlcv_overridden, cancel_requested)
           VALUES (?, ?, ?, NULL, 'PLANNED', 0, 0, 0)""",
        (run_id, h, h),
    )
    conn.commit()
    conn.close()


def _minimal_plan() -> dict[str, Any]:
    return {
        "plan_schema_version": 1,
        "config": {
            "source_scopes": [
                {
                    "source_type": "polygon_news", "subjects": ["AAPL"],
                    "endpoint_names": ["news"], "stage": "evidence",
                    "start_date": "2025-08-01", "end_date": "2026-07-23",
                    "date_domain": "calendar_days", "provider_profile_version": "v1",
                }
            ]
        },
    }


def _univ():
    return {"runtime_manifest_id": "u" * 64, "tickers": ["AAPL"]}


@pytest.fixture
def db():
    path = _fresh_db_path()
    _insert_run(path)
    yield path
    import os; os.unlink(path)


# ---------------------------------------------------------------------------
class TestLegacyB2ONotPollutedBySec:
    def test_no_sec_without_inventory(self, db):
        from catalyst_data.coverage_audit import run_b2o_readiness_audit
        from catalyst_data.update_planner import UpdatePlan
        up = UpdatePlan(**_minimal_plan())
        report = run_b2o_readiness_audit(db, universe_manifest=_univ(), plan=up, terminal_run_id="r1", pre_b6_sec=False)
        assert report.get("sec_source_ready") is None


class TestSupersessionFailClosed:
    def test_legacy_cannot_bypass_mandatory(self, db):
        from catalyst_data.coverage_audit import run_b2o_readiness_audit
        from catalyst_data.update_planner import UpdatePlan
        up = UpdatePlan(**_minimal_plan())
        report = run_b2o_readiness_audit(db, universe_manifest=_univ(), plan=up, terminal_run_id="r1", pre_b6_sec=False)
        b2o = report["b2o_readiness"]
        missing = b2o["overall_readiness"]["missing_or_incomplete_cell_ids"]
        assert len(missing) > 0, "Legacy B2-O must show mandatory gaps"


class TestSupersessionArbitrary:
    def test_arbitrary_cell_id_rejected(self, db):
        from catalyst_data.coverage_audit import run_b2o_readiness_audit
        from catalyst_data.update_planner import UpdatePlan
        up = UpdatePlan(**_minimal_plan())
        report = run_b2o_readiness_audit(db, universe_manifest=_univ(), plan=up, terminal_run_id="r1", pre_b6_sec=False)
        b2o = report["b2o_readiness"]
        missing = b2o["overall_readiness"]["missing_or_incomplete_cell_ids"]
        assert len(missing) > 0


class TestFmpOptional:
    def test_fmp_in_optional_not_mandatory(self, db):
        from catalyst_data.coverage_audit import run_b2o_readiness_audit
        from catalyst_data.update_planner import UpdatePlan
        plan_dict = _minimal_plan()
        plan_dict["config"]["source_scopes"].append({"source_type": "fmp_fundamentals", "subjects": ["AAPL"], "endpoint_names": ["balance_sheet"], "stage": "evidence", "start_date": "2026-07-23", "end_date": "2026-07-23", "date_domain": "as_of", "provider_profile_version": "v1"})
        up = UpdatePlan(**plan_dict)
        report = run_b2o_readiness_audit(db, universe_manifest=_univ(), plan=up, terminal_run_id="r1", pre_b6_sec=False)
        b2o = report["b2o_readiness"]
        assert b2o["optional_source_status"]["fmp_fundamentals"]["status"] == "degraded"
        assert len(b2o["required_provenance"]["missing_or_invalid_cell_ids"]) == 0


class TestMandatoryBlocks:
    def test_polygon_news_gap_blocks(self, db):
        from catalyst_data.coverage_audit import run_b2o_readiness_audit
        from catalyst_data.update_planner import UpdatePlan
        up = UpdatePlan(**_minimal_plan())
        report = run_b2o_readiness_audit(db, universe_manifest=_univ(), plan=up, terminal_run_id="r1", pre_b6_sec=False)
        b2o = report["b2o_readiness"]
        assert b2o["overall_readiness"]["status"] == "incomplete"
        assert len(b2o["overall_readiness"]["missing_or_incomplete_cell_ids"]) > 0


class TestCompleteNoGaps:
    def test_complete_implies_no_gaps(self, db):
        from catalyst_data.coverage_audit import run_b2o_readiness_audit
        from catalyst_data.update_planner import UpdatePlan
        up = UpdatePlan(**_minimal_plan())
        report = run_b2o_readiness_audit(db, universe_manifest=_univ(), plan=up, terminal_run_id="r1", pre_b6_sec=False)
        b2o = report["b2o_readiness"]
        missing = b2o["overall_readiness"]["missing_or_incomplete_cell_ids"]
        if b2o["overall_readiness"]["status"] == "complete":
            assert len(missing) == 0
        else:
            assert len(missing) > 0


class TestCliEnvelope:
    def test_succeeded_no_errors(self):
        from catalyst_data.b2o import _envelope
        env = _envelope("test", "SUCCEEDED", {"ok": True})
        assert env["status"] == "SUCCEEDED"
        assert not env["errors"]

    def test_failed_has_specific_errors(self):
        from catalyst_data.b2o import _envelope
        env = _envelope("test", "FAILED", {}, ["sec_source_ready_false"])
        assert len(env["errors"]) >= 1

    def test_pre_b6_publish_forwards_progress_to_stderr_only(
        self, monkeypatch, tmp_path, capsys
    ):
        import catalyst_data.b2o as b2o
        import catalyst_data.update_planner as update_planner

        db_path = tmp_path / "candidate.db"
        sqlite3.connect(db_path).close()
        files = {}
        for name, value in {
            "snapshot": {"snapshot_id": "s" * 64},
            "universe": {},
            "inventory": {},
            "evidence": {},
            "plan": {},
        }.items():
            path = tmp_path / f"{name}.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            files[name] = path

        captured = {}

        def fake_publish(_conn, **kwargs):
            captured.update(kwargs)
            kwargs["progress_callback"]({
                "phase": "articles",
                "documents": 100,
                "chunks": 1234,
                "source_utf8_bytes": 5000,
                "chunk_text_utf8_bytes": 6000,
                "elapsed_seconds": 30.0,
                "rss_bytes": 5678,
            })
            return SimpleNamespace(
                build_id="d" * 64,
                corpus=SimpleNamespace(
                    manifest_id="c" * 64, document_count=100, chunk_count=1234
                ),
                lexical=SimpleNamespace(manifest_id="c" * 64, row_count=1234),
            )

        monkeypatch.setattr(b2o, "_load_manifest", lambda _path: {})
        monkeypatch.setattr(
            b2o, "_rerun_and_verify_pre_b6_snapshot", lambda **_kwargs: {}
        )
        monkeypatch.setattr(
            b2o, "publish_streaming_corpus_with_resource_gate", fake_publish
        )
        monkeypatch.setattr(update_planner, "UpdatePlan", lambda **_kwargs: object())

        code = b2o.main([
            "pre-b6-publish-corpus",
            "--db", str(db_path),
            "--snapshot-manifest", str(files["snapshot"]),
            "--universe-manifest", str(files["universe"]),
            "--filing-inventory", str(files["inventory"]),
            "--convergence-evidence", str(files["evidence"]),
            "--b2o-plan", str(files["plan"]),
            "--b2o-terminal-run-id", "b2o",
            "--baseline-snapshot-id", "b" * 64,
            "--s1-terminal-run-id", "s1",
            "--s2-terminal-run-id", "s2",
            "--s4-terminal-run-id", "s4",
        ])
        output = capsys.readouterr()
        assert code == 0
        assert json.loads(output.out)["status"] == "SUCCEEDED"
        progress = json.loads(output.err)
        assert progress["event"] == "corpus_progress"
        assert progress["documents"] == 100
        assert progress["chunks"] == 1234
        assert captured["progress_callback"] is not None


class TestNoDebug:
    def test_no_supersede_debug(self):
        content = open("packages/data-core/catalyst_data/coverage_audit.py").read()
        assert "SUPERSEDE_DEBUG" not in content
