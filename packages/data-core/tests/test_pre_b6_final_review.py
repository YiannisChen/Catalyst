"""Regression tests for the final Pre-B6 readiness review."""

from __future__ import annotations

import inspect
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from catalyst_data.b2o import main
from catalyst_data.manifests.universe import (
    RATIFIED_TICKERS,
    SourceWindows,
    load_universe_spec,
)
from catalyst_data.retrieval.source_bundle import export_source_bundle
from catalyst_data.sec.inventory import build_filing_inventory_manifest
from catalyst_data.sec.readiness import (
    SecReadinessError,
    load_and_verify_convergence_evidence,
)


SPEC_PATH = (
    Path(__file__).parents[1]
    / "catalyst_data"
    / "manifests"
    / "universe_v1_2025_08.spec.json"
)


def _issuer_classes() -> dict[str, str]:
    spec = load_universe_spec(SPEC_PATH)
    return {
        ticker: str(spec.companies[ticker]["issuer_class"])
        for ticker in spec.tickers
    }


def _convergence_evidence(tmp_path: Path, **overrides: object) -> Path:
    from catalyst_data.sec.convergence_identity import compute_convergence_plan_hash

    body: dict[str, object] = {
        "baseline_snapshot_id": "b" * 64,
        "universe_manifest_id": "a" * 64,
        "s1_plan_hash": "1" * 64,
        "s2_plan_hash": "2" * 64,
        "inventory_id": "c" * 64,
        "s4_plan_hash": "4" * 64,
        "reconciliation_evidence_hash": "d" * 64,
        "db_user_version": 13,
        "readiness_policy_version": "b2e_readiness_v1",
    }
    body.update(overrides)
    body["convergence_plan_hash"] = compute_convergence_plan_hash(
        baseline_snapshot_id=str(body["baseline_snapshot_id"]),
        universe_manifest_id=str(body["universe_manifest_id"]),
        s1_plan_hash=str(body["s1_plan_hash"]),
        s2_plan_hash=str(body["s2_plan_hash"]),
        inventory_id=str(body["inventory_id"]),
        s4_plan_hash=str(body["s4_plan_hash"]),
        reconciliation_evidence_hash=str(body["reconciliation_evidence_hash"]),
        db_user_version=int(body["db_user_version"]),
        readiness_policy_version=str(body["readiness_policy_version"]),
    )
    path = tmp_path / "convergence.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("command", "command_args"),
    [
        ("pre-b6-audit", []),
        (
            "pre-b6-snapshot",
            [
                "--protected-source-sha256",
                "9" * 64,
                "--output",
                "snapshot.json",
            ],
        ),
    ],
)
def test_pre_b6_cli_requires_b2o_plan_and_terminal_lineage(
    command: str, command_args: list[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(
            [
                command,
                "--db",
                "candidate.db",
                "--universe-manifest",
                "universe.json",
                "--filing-inventory",
                "inventory.json",
                "--convergence-evidence",
                "convergence.json",
                "--s1-terminal-run-id",
                "s1",
                "--s2-terminal-run-id",
                "s2",
                "--s4-terminal-run-id",
                "s4",
                "--baseline-snapshot-id",
                "b" * 64,
                *command_args,
            ]
        )
    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"universe_manifest_id": "x" * 64}, "universe_manifest_id"),
        ({"baseline_snapshot_id": "x" * 64}, "baseline_snapshot_id"),
        ({"db_user_version": 12}, "db_user_version"),
        ({"readiness_policy_version": "wrong"}, "readiness_policy_version"),
    ],
)
def test_convergence_evidence_rejects_runtime_identity_drift(
    tmp_path: Path,
    override: dict[str, object],
    message: str,
) -> None:
    path = _convergence_evidence(tmp_path, **override)
    with pytest.raises(SecReadinessError, match=message):
        load_and_verify_convergence_evidence(
            path,
            expected_universe_manifest_id="a" * 64,
            expected_baseline_snapshot_id="b" * 64,
            expected_db_user_version=13,
            expected_readiness_policy_version="b2e_readiness_v1",
        )


def test_source_bundle_public_api_has_no_gate_bypass() -> None:
    assert "require_production_gates" not in inspect.signature(
        export_source_bundle
    ).parameters


def test_inventory_builder_requires_ratified_universe() -> None:
    with pytest.raises(TypeError):
        build_filing_inventory_manifest(
            source_snapshot_id="s" * 64,
            universe_manifest_id="u" * 64,
            filing_entries=[],
        )


def test_inventory_builder_rejects_non_ratified_universe() -> None:
    tickers = list(RATIFIED_TICKERS)
    tickers[-1] = "FAKE"
    with pytest.raises(ValueError, match="ratified 40"):
        build_filing_inventory_manifest(
            source_snapshot_id="s" * 64,
            universe_manifest_id="u" * 64,
            filing_entries=[],
            tickers=tickers,
            issuer_class_by_ticker=_issuer_classes(),
        )


def test_inventory_builder_cannot_override_computed_carry_in() -> None:
    with pytest.raises(TypeError):
        build_filing_inventory_manifest(
            source_snapshot_id="s" * 64,
            universe_manifest_id="u" * 64,
            filing_entries=[],
            tickers=list(RATIFIED_TICKERS),
            issuer_class_by_ticker=_issuer_classes(),
            missing_carry_in_slots=[],
        )


def test_source_bundle_without_real_gates_cannot_export(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    with pytest.raises(TypeError, match="probe_report_path"):
        export_source_bundle(
            conn,
            corpus_manifest_id="m" * 64,
            snapshot_id="s" * 64,
            output_dir=tmp_path,
        )


def _boot_v13(path: Path) -> sqlite3.Connection:
    from catalyst_data.migrations import run_migrations
    from catalyst_data.storage.sqlite import init_db

    conn = sqlite3.connect(path)
    init_db(conn)
    run_migrations(conn)
    return conn


def test_b2o_news_incomplete_keeps_pre_b6_overall_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import catalyst_data.coverage_audit as audit
    import catalyst_data.sec.readiness as sec

    db = tmp_path / "candidate.db"
    conn = _boot_v13(db)
    for run_id, plan_hash in [
        ("b2o", "f" * 64),
        ("s1", "1" * 64),
        ("s2", "2" * 64),
        ("s4", "4" * 64),
    ]:
        conn.execute(
            """INSERT INTO ingestion_runs (
                run_id, started_at, ticker_list_json, source_list_json, status,
                plan_hash, expected_plan_hash, allow_stale_ohlcv,
                allow_stale_ohlcv_overridden, cancel_requested
            ) VALUES (?, '2026-01-01T00:00:00Z', '[]', '[]', 'PLANNED',
                      ?, ?, 0, 0, 0)""",
            (run_id, plan_hash, plan_hash),
        )
        conn.execute(
            "UPDATE ingestion_runs SET status='SUCCEEDED' WHERE run_id=?",
            (run_id,),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        sec,
        "load_frozen_filing_inventory",
            lambda *_a, **_k: {
                "inventory_id": "c" * 64,
                "universe_manifest_id": "a" * 64,
                "source_snapshot_id": "b" * 64,
                "index_cell_ids": ["idx"],
            "missing_carry_in_slots": [],
            "sorted_filing_entries": [],
        },
    )
    monkeypatch.setattr(
        sec,
        "load_and_verify_convergence_evidence",
        lambda *_a, **_k: (
            "e" * 64,
            {
                "s1_plan_hash": "1" * 64,
                "s2_plan_hash": "2" * 64,
                "s4_plan_hash": "4" * 64,
            },
        ),
    )
    monkeypatch.setattr(sec, "mandatory_document_ids_from_inventory", lambda _i: (["d"], []))

    def fake_attach(report, _conn, **_kwargs):
        report["sec_source_ready"] = True
        report["sec_evidence_ready"] = True
        report["sec_readiness"] = {
            "sec_source_ready": True,
            "sec_evidence_ready": True,
        }
        return report

    monkeypatch.setattr(audit, "attach_sec_readiness", fake_attach)
    report = audit.run_pre_b6_sec_readiness_audit(
        str(db),
        universe_manifest={
            "runtime_manifest_id": "a" * 64,
            "tickers": list(RATIFIED_TICKERS),
        },
        b2o_plan=SimpleNamespace(
            plan_hash="f" * 64,
            expected_plan_hash="f" * 64,
            config={
                "source_scopes": [
                    {
                        "source_type": "polygon_news",
                        "subjects": ["AAPL"],
                        "endpoint_names": ["news"],
                        "stage": "evidence",
                        "start_date": "2025-08-01",
                        "end_date": "2026-07-23",
                        "date_domain": "calendar_days",
                        "provider_profile_version": "v1",
                    }
                ]
            },
        ),
        b2o_terminal_run_id="b2o",
        baseline_snapshot_id="b" * 64,
        filing_inventory_path="inventory.json",
        convergence_evidence_path="convergence.json",
        s1_terminal_run_id="s1",
        s2_terminal_run_id="s2",
        s4_terminal_run_id="s4",
    )
    assert (
        report["b2o_readiness"]["canonical_news_comparable_gate"]["status"]
        == "incomplete"
    )
    assert report["sec_source_ready"] is True
    assert report["sec_evidence_ready"] is True
    assert report["b2o_readiness"]["overall_readiness"]["status"] == "incomplete"
    assert (
        report["b2o_readiness"]["readiness_binding"]["b2o_terminal_run_id"]
        == "b2o"
    )


@pytest.mark.parametrize("endpoint", ["sec_document", "sec_filing_index"])
def test_pre_b6_snapshot_reverification_detects_request_ledger_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    import catalyst_data.b2o as b2o
    import catalyst_data.coverage_audit as audit
    from catalyst_data.manifests.snapshot import build_data_snapshot_manifest

    db = tmp_path / "candidate.db"
    conn = _boot_v13(db)
    binding = {
        "b2o_terminal_run_id": "b2o",
        "b2o_plan_hash": "f" * 64,
        "s1_terminal_run_id": "s1",
        "s2_terminal_run_id": "s2",
        "s4_terminal_run_id": "s4",
        "inventory_id": "c" * 64,
        "baseline_snapshot_id": "b" * 64,
        "universe_manifest_id": "a" * 64,
        "db_user_version": 13,
        "readiness_policy_version": "b2e_readiness_v1",
        "convergence_plan_hash": "e" * 64,
    }
    readiness = {
        "convergence_plan_hash": "e" * 64,
        "sec_source_ready": True,
        "sec_evidence_ready": False,
        "prebuild_source_ready": True,
        "postbuild_evidence_ready": False,
        "sec_readiness": {"sec_source_ready": True, "chunked_count": 0},
        "b2o_readiness": {
            "overall_readiness": {"status": "complete"},
            "canonical_news_comparable_gate": {"status": "complete"},
            "comparable_gate": {"status": "complete"},
            "sec_source_ready": True,
            "readiness_binding": binding,
        },
    }
    coverage = {
        **readiness["b2o_readiness"],
        "sec_source_ready": True,
        "sec_evidence_ready": False,
        "sec_readiness": readiness["sec_readiness"],
    }
    windows = SourceWindows(
        degraded_start="2024-01-01",
        degraded_end="2025-07-31",
        canonical_start="2025-08-01",
        canonical_end="2026-07-23",
    )
    snapshot = build_data_snapshot_manifest(
        conn,
        universe_manifest_id="a" * 64,
        plan_hash="e" * 64,
        protected_source_sha256="9" * 64,
        source_windows=windows,
        coverage_states=coverage,
        created_at=datetime.now(timezone.utc),
        convergence_plan_hash="e" * 64,
    ).to_dict()
    conn.execute(
        """INSERT INTO ingestion_runs (
            run_id, started_at, ticker_list_json, source_list_json, status,
            plan_hash, expected_plan_hash, allow_stale_ohlcv,
            allow_stale_ohlcv_overridden, cancel_requested
        ) VALUES ('drift-run','2026-01-01T00:00:00Z','[]','[]','PLANNED',
                  ?,?,0,0,0)""",
        ("f" * 64, "f" * 64),
    )
    conn.execute(
        """INSERT INTO provider_request_attempts (
            request_id, run_id, logical_fetch_id, source_type, provider,
            endpoint_name, ticker_or_series, window_start, window_end,
            attempt_no, page_no, request_fingerprint, request_params_redacted,
            started_at, status
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "1" * 64,
            "drift-run",
            "2" * 64,
            "sec_filings",
            "sec",
            endpoint,
            "AAPL",
            "2025-08-01",
            "2025-08-01",
            1,
            1,
            "3" * 64,
            "{}",
            "2026-01-01T00:00:00Z",
            "STARTED",
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        audit, "run_pre_b6_sec_readiness_audit", lambda *_a, **_k: readiness
    )
    with pytest.raises(ValueError, match="current DB/readiness state"):
        b2o._rerun_and_verify_pre_b6_snapshot(
            db_path=str(db),
            snapshot=snapshot,
            universe_manifest={
                "runtime_manifest_id": "a" * 64,
                "source_windows": windows.to_identity(),
            },
            b2o_plan=SimpleNamespace(plan_hash="f" * 64),
            b2o_terminal_run_id="b2o",
            baseline_snapshot_id="b" * 64,
            filing_inventory_path="inventory.json",
            convergence_evidence_path="convergence.json",
            s1_terminal_run_id="s1",
            s2_terminal_run_id="s2",
            s4_terminal_run_id="s4",
            expected_inventory_id=None,
            s2_index_cell_ids=None,
        )


def test_pre_b6_snapshot_reverification_allows_postbuild_gate_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import catalyst_data.b2o as b2o
    import catalyst_data.coverage_audit as audit
    from catalyst_data.manifests.snapshot import build_data_snapshot_manifest

    db = tmp_path / "candidate.db"
    conn = _boot_v13(db)
    windows = SourceWindows(
        degraded_start="2024-01-01",
        degraded_end="2025-07-31",
        canonical_start="2025-08-01",
        canonical_end="2026-07-23",
    )
    binding = {
        "b2o_terminal_run_id": "b2o",
        "s1_terminal_run_id": "s1",
        "s2_terminal_run_id": "s2",
        "s4_terminal_run_id": "s4",
        "inventory_id": "c" * 64,
        "baseline_snapshot_id": "b" * 64,
        "universe_manifest_id": "a" * 64,
        "convergence_plan_hash": "e" * 64,
        "postbuild_evidence_ready": False,
    }
    prebuild_coverage = {
        "overall_readiness": {"status": "complete"},
        "canonical_news_comparable_gate": {"status": "complete"},
        "comparable_gate": {"status": "complete"},
        "sec_source_ready": True,
        "sec_evidence_ready": False,
        "sec_readiness": {"chunked_count": 0},
        "readiness_binding": binding,
    }
    snapshot = build_data_snapshot_manifest(
        conn,
        universe_manifest_id="a" * 64,
        plan_hash="e" * 64,
        protected_source_sha256="9" * 64,
        source_windows=windows,
        coverage_states=prebuild_coverage,
        created_at=datetime.now(timezone.utc),
        convergence_plan_hash="e" * 64,
    ).to_dict()
    conn.close()

    postbuild_readiness = {
        "convergence_plan_hash": "e" * 64,
        "sec_source_ready": True,
        "sec_evidence_ready": True,
        "prebuild_source_ready": True,
        "postbuild_evidence_ready": True,
        "sec_readiness": {"chunked_count": 688},
        "b2o_readiness": {
            "overall_readiness": {"status": "complete"},
            "canonical_news_comparable_gate": {"status": "complete"},
            "comparable_gate": {"status": "complete"},
            "readiness_binding": {
                **binding,
                "postbuild_evidence_ready": True,
            },
        },
    }
    monkeypatch.setattr(
        audit,
        "run_pre_b6_sec_readiness_audit",
        lambda *_args, **_kwargs: postbuild_readiness,
    )

    result = b2o._rerun_and_verify_pre_b6_snapshot(
        db_path=str(db),
        snapshot=snapshot,
        universe_manifest={
            "runtime_manifest_id": "a" * 64,
            "source_windows": windows.to_identity(),
        },
        b2o_plan=SimpleNamespace(plan_hash="f" * 64),
        b2o_terminal_run_id="b2o",
        baseline_snapshot_id="b" * 64,
        filing_inventory_path="inventory.json",
        convergence_evidence_path="convergence.json",
        s1_terminal_run_id="s1",
        s2_terminal_run_id="s2",
        s4_terminal_run_id="s4",
        expected_inventory_id=None,
        s2_index_cell_ids=None,
    )
    assert result is postbuild_readiness


def test_legacy_snapshot_verifier_rejects_forged_pre_b6_coverage() -> None:
    import catalyst_data.b2o as b2o

    conn = sqlite3.connect(":memory:")
    with pytest.raises(ValueError, match="explicit pre-b6"):
        b2o._verify_snapshot_manifest_for_db(
            conn,
            {
                "coverage_states": {
                    "overall_readiness": {"status": "complete"},
                    "canonical_news_comparable_gate": {"status": "complete"},
                    "sec_source_ready": True,
                }
            },
        )
