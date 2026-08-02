"""Pre-B6 promotion front-door gates required before GPU export."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from catalyst_data.b2o import main
from catalyst_data.manifests.operations import PromotionResult
from catalyst_data.manifests.universe import RATIFIED_TICKERS


MID = "c" * 64
SID = "5" * 64
UID = "6" * 64
CONVERGENCE = "e" * 64
CUTOFF = "2025-12-31T00:00:00Z"


def _write_postbuild_readiness(
    path: Path,
    *,
    ready: bool = True,
    mandatory_document_ids: list[str] | None = None,
    universe_manifest_id: str = UID,
    snapshot_id: str = SID,
) -> Path:
    from catalyst_data.manifests.universe import sha256_identity

    document_ids = mandatory_document_ids or [
        f"document_{ordinal:02d}" for ordinal in range(40)
    ]
    readiness = {
        "schema_version": "pre_b6_postbuild_readiness_v1",
        "postbuild_evidence_ready": ready,
        "universe_manifest_id": universe_manifest_id,
        "inventory_id": "1" * 64,
        "source_snapshot_id": "b" * 64,
        "snapshot_id": snapshot_id,
        "corpus_manifest_id": MID,
        "lexical_manifest_id": MID,
        "b2o_terminal_run_id": "b2o",
        "s1_terminal_run_id": "s1",
        "s2_terminal_run_id": "s2",
        "s4_terminal_run_id": "s4",
        "mandatory_document_ids": document_ids,
        "expected_mandatory_count": len(document_ids),
        "chunked_count": len(document_ids) if ready else 0,
    }
    readiness["postbuild_readiness_id"] = sha256_identity(readiness)
    binding = {
        key: readiness[key]
        for key in (
            "b2o_terminal_run_id", "s1_terminal_run_id", "s2_terminal_run_id",
            "s4_terminal_run_id", "inventory_id", "universe_manifest_id",
            "source_snapshot_id",
        )
    }
    path.write_text(
        json.dumps({
            "postbuild_readiness": readiness,
            "b2o_readiness": {"readiness_binding": binding},
        }),
        encoding="utf-8",
    )
    return path


def _boot_probe_db(path: Path, *, ticker_count: int) -> None:
    from catalyst_data.migrations import run_migrations
    from catalyst_data.retrieval.fts5_builder import build_fts5_index
    from catalyst_data.storage.sqlite import init_db

    conn = sqlite3.connect(path)
    init_db(conn)
    run_migrations(conn)
    conn.execute(
        """INSERT INTO corpus_manifest
           (manifest_id, manifest_json, is_current, created_at)
           VALUES (?, ?, 1, '2026-01-01T00:00:00Z')""",
        (
            MID,
            json.dumps(
                {
                    "manifest_id": MID,
                    "certified_snapshot_identity": SID,
                }
            ),
        ),
    )
    body = (
        "quarterly earnings guidance revenue growth product demand outlook "
        "operating margin"
    )
    for ordinal, ticker in enumerate(list(RATIFIED_TICKERS)[:ticker_count]):
        conn.execute(
            """INSERT INTO corpus_chunks (
                chunk_id, document_id, chunk_profile_version, section_key, ordinal,
                content_text, content_hash, metadata_hash, source_class,
                available_at, ticker_associations, eligibility, manifest_id, status,
                boundary_kind, body_token_start, body_token_end, body_overlap_tokens,
                prefix_token_count, prefix_truncated, section_parse_degraded,
                created_at, updated_at
            ) VALUES (?, ?, 'filing_v3', 'section', ?, ?, ?, ?,
                'official_government', '2025-08-01T00:00:00Z', ?, 'eligible',
                ?, 'pending_embedding', 'document_end', 0, 10, 0, 0, 0, 0,
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
            (
                f"chunk_{ordinal:02d}",
                f"document_{ordinal:02d}",
                f"{ordinal:04d}",
                body,
                hashlib.sha256(body.encode("utf-8")).hexdigest(),
                hashlib.sha256(f"metadata-{ordinal}".encode("utf-8")).hexdigest(),
                json.dumps([ticker]),
                MID,
            ),
        )
    conn.commit()
    build_fts5_index(conn, MID, clock=lambda: "2026-01-02T00:00:00Z")
    conn.close()


def _write_cli_inputs(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "snapshot": tmp_path / "snapshot.json",
        "universe": tmp_path / "universe.json",
        "plan": tmp_path / "plan.json",
        "inventory": tmp_path / "inventory.json",
        "convergence": tmp_path / "convergence.json",
        "probe": tmp_path / "probe.json",
    }
    paths["snapshot"].write_text(
        json.dumps({"snapshot_id": SID, "plan_hash": CONVERGENCE}),
        encoding="utf-8",
    )
    paths["universe"].write_text(
        json.dumps(
            {
                "runtime_manifest_id": UID,
                "tickers": list(RATIFIED_TICKERS),
                "source_windows": {
                    "degraded_start": "2024-01-01",
                    "degraded_end": "2025-07-31",
                    "canonical_start": "2025-08-01",
                    "canonical_end": "2026-07-23",
                },
            }
        ),
        encoding="utf-8",
    )
    paths["plan"].write_text(
        json.dumps({"plan_hash": "f" * 64}), encoding="utf-8"
    )
    paths["inventory"].write_text("{}", encoding="utf-8")
    paths["convergence"].write_text("{}", encoding="utf-8")
    return paths


def _promote_argv(db: Path, paths: dict[str, Path]) -> list[str]:
    return [
        "pre-b6-promote",
        "--db",
        str(db),
        "--snapshot-manifest",
        str(paths["snapshot"]),
        "--universe-manifest",
        str(paths["universe"]),
        "--filing-inventory",
        str(paths["inventory"]),
        "--convergence-evidence",
        str(paths["convergence"]),
        "--b2o-plan",
        str(paths["plan"]),
        "--b2o-terminal-run-id",
        "b2o",
        "--baseline-snapshot-id",
        "b" * 64,
        "--s1-terminal-run-id",
        "s1",
        "--s2-terminal-run-id",
        "s2",
        "--s4-terminal-run-id",
        "s4",
        "--probe-cutoff",
        CUTOFF,
        "--probe-output",
        str(paths["probe"]),
        "--snapshots-dir",
        str(db.parent / "snapshots"),
        "--active-pointer",
        str(db.parent / "active.json"),
    ]


def _patch_source_snapshot_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    import catalyst_data.b2o as b2o

    monkeypatch.setattr(
        b2o,
        "_rerun_and_verify_pre_b6_snapshot",
        lambda **_kwargs: {
            "convergence_plan_hash": CONVERGENCE,
            "sec_source_ready": True,
            "sec_evidence_ready": False,
            "prebuild_source_ready": True,
            "postbuild_evidence_ready": False,
            "sec_readiness": {"chunked_count": 0},
            "b2o_readiness": {
                "overall_readiness": {"status": "complete"},
            },
        },
    )


def _patch_evidence_audit(
    monkeypatch: pytest.MonkeyPatch, *, evidence_ready: bool
) -> None:
    import catalyst_data.coverage_audit as audit

    def fake_audit(*args, **kwargs):
        report = {
            "convergence_plan_hash": CONVERGENCE,
            "sec_source_ready": True,
            "sec_evidence_ready": evidence_ready,
            "prebuild_source_ready": True,
            "postbuild_evidence_ready": evidence_ready,
            "sec_readiness": {"chunked_count": 688 if evidence_ready else 0},
            "b2o_readiness": {
                "overall_readiness": {"status": "complete"},
                "readiness_binding": {
                    "universe_manifest_id": UID,
                    "convergence_plan_hash": CONVERGENCE,
                },
            },
        }
        if evidence_ready:
            conn = sqlite3.connect(args[0])
            document_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT document_id FROM corpus_chunks ORDER BY document_id"
                )
            ]
            conn.close()
            output_dir = Path(kwargs["output_dir"])
            output_dir.mkdir(parents=True, exist_ok=True)
            report_path = _write_postbuild_readiness(
                output_dir / "pre_b6_sec_readiness_audit.json",
                mandatory_document_ids=document_ids,
            )
            report["report_path"] = str(report_path)
            report["postbuild_readiness"] = json.loads(
                report_path.read_text(encoding="utf-8")
            )["postbuild_readiness"]
        return report

    monkeypatch.setattr(audit, "run_pre_b6_sec_readiness_audit", fake_audit)


def test_pre_b6_promote_rejects_sec_evidence_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import catalyst_data.b2o as b2o

    db = tmp_path / "candidate.db"
    _boot_probe_db(db, ticker_count=40)
    paths = _write_cli_inputs(tmp_path)
    _patch_source_snapshot_gate(monkeypatch)
    _patch_evidence_audit(monkeypatch, evidence_ready=False)
    called = False

    def forbidden_promote(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("promote_candidate must not run")

    monkeypatch.setattr(b2o, "promote_candidate", forbidden_promote)
    assert main(_promote_argv(db, paths)) == 7
    assert called is False
    envelope = json.loads(capsys.readouterr().out)
    assert "chunked_count=0" in envelope["errors"][0]


def test_pre_b6_promote_rejects_39_of_40_probe_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import catalyst_data.b2o as b2o

    db = tmp_path / "candidate.db"
    _boot_probe_db(db, ticker_count=39)
    paths = _write_cli_inputs(tmp_path)
    _patch_source_snapshot_gate(monkeypatch)
    _patch_evidence_audit(monkeypatch, evidence_ready=True)
    called = False

    def forbidden_promote(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("promote_candidate must not run")

    monkeypatch.setattr(b2o, "promote_candidate", forbidden_promote)
    assert main(_promote_argv(db, paths)) == 7
    assert called is False
    report = json.loads(paths["probe"].read_text(encoding="utf-8"))
    assert report["overall_pass"] is False
    assert len(report["coverage_results"]) == 40
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["data"]["probe_report_id"] == report["probe_report_id"]
    assert envelope["data"]["probe_report_path"] == str(paths["probe"].resolve())


def test_pre_b6_promote_accepts_only_evidence_and_dual_40_of_40_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import catalyst_data.b2o as b2o

    db = tmp_path / "candidate.db"
    _boot_probe_db(db, ticker_count=40)
    paths = _write_cli_inputs(tmp_path)
    _patch_source_snapshot_gate(monkeypatch)
    _patch_evidence_audit(monkeypatch, evidence_ready=True)
    called = False

    def fake_promote(**_kwargs):
        nonlocal called
        called = True
        return PromotionResult(
            final_path=tmp_path / "final.db",
            active_pointer_path=tmp_path / "active.json",
            snapshot_id=SID,
            final_sha256="9" * 64,
        )

    monkeypatch.setattr(b2o, "promote_candidate", fake_promote)
    assert main(_promote_argv(db, paths)) == 0
    assert called is True
    report = json.loads(paths["probe"].read_text(encoding="utf-8"))
    assert report["overall_pass"] is True
    assert report["coverage_pass_count"] == 40
    assert report["lexical_pass_count"] == 40
    assert report["probe_report_id"]


def test_probe_gate_rejects_future_available_at(
    tmp_path: Path,
) -> None:
    from catalyst_data.pre_b6_probes import run_pre_b6_probe_gates

    db = tmp_path / "candidate.db"
    _boot_probe_db(db, ticker_count=40)
    conn = sqlite3.connect(db)
    conn.execute(
        """UPDATE corpus_chunks SET available_at='2026-01-01T00:00:00Z'
           WHERE ticker_associations='["AAPL"]'"""
    )
    conn.commit()
    readiness_path = _write_postbuild_readiness(tmp_path / "postbuild.json")
    with pytest.raises(ValueError, match="40/40"):
        run_pre_b6_probe_gates(
            conn,
            universe_manifest_id=UID,
            snapshot_id=SID,
            corpus_manifest_id=MID,
            ordered_tickers=list(RATIFIED_TICKERS),
            probe_cutoff=CUTOFF,
            output_path=tmp_path / "probe.json",
            postbuild_readiness_report_path=readiness_path,
        )
    conn.close()


def test_direct_probe_rejects_40_of_40_when_one_mandatory_document_is_unchunked(
    tmp_path: Path,
) -> None:
    from catalyst_data.pre_b6_probes import run_pre_b6_probe_gates

    db = tmp_path / "candidate.db"
    _boot_probe_db(db, ticker_count=40)
    readiness_path = _write_postbuild_readiness(
        tmp_path / "postbuild.json",
        mandatory_document_ids=[
            *(f"document_{ordinal:02d}" for ordinal in range(40)),
            "mandatory_document_without_chunks",
        ],
    )
    conn = sqlite3.connect(db)
    with pytest.raises(ValueError, match="mandatory SEC document"):
        run_pre_b6_probe_gates(
            conn,
            universe_manifest_id=UID,
            snapshot_id=SID,
            corpus_manifest_id=MID,
            ordered_tickers=list(RATIFIED_TICKERS),
            probe_cutoff=CUTOFF,
            output_path=tmp_path / "probe.json",
            postbuild_readiness_report_path=readiness_path,
        )
    assert not (tmp_path / "probe.json").exists()
    conn.close()


def test_source_bundle_accepts_real_report_recomputed_from_current_db(
    tmp_path: Path,
) -> None:
    from catalyst_data.pre_b6_probes import run_pre_b6_probe_gates
    from catalyst_data.retrieval.source_bundle import export_source_bundle

    db = tmp_path / "candidate.db"
    report_path = tmp_path / "probe.json"
    _boot_probe_db(db, ticker_count=40)
    conn = sqlite3.connect(db)
    readiness_path = _write_postbuild_readiness(tmp_path / "postbuild.json")
    report = run_pre_b6_probe_gates(
        conn,
        universe_manifest_id=UID,
        snapshot_id=SID,
        corpus_manifest_id=MID,
        ordered_tickers=list(RATIFIED_TICKERS),
        probe_cutoff=CUTOFF,
        output_path=report_path,
        postbuild_readiness_report_path=readiness_path,
    )
    changes_before_export = conn.total_changes

    source_bundle_id = export_source_bundle(
        conn,
        universe_manifest_id=UID,
        corpus_manifest_id=MID,
        snapshot_id=SID,
        probe_cutoff=CUTOFF,
        output_dir=tmp_path / "bundles",
        probe_report_path=report_path,
        postbuild_readiness_report_path=readiness_path,
    )
    assert conn.total_changes == changes_before_export

    manifest = json.loads(
        (
            tmp_path
            / "bundles"
            / f"source_{source_bundle_id}"
            / "source_bundle_manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["probe_report_id"] == report["probe_report_id"]
    conn.close()
