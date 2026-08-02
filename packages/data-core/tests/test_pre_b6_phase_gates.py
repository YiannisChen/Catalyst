"""RED contract tests for explicit Pre-B6 prebuild/postbuild gates."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from catalyst_data.b2o import main
from catalyst_data.manifests.universe import RATIFIED_TICKERS


def _readiness_report(*, evidence_ready: bool = False) -> dict:
    prebuild_source_ready = True
    postbuild_evidence_ready = evidence_ready
    return {
        "sec_source_ready": True,
        "sec_evidence_ready": evidence_ready,
        "prebuild_source_ready": prebuild_source_ready,
        "postbuild_evidence_ready": postbuild_evidence_ready,
        "sec_readiness": {
            "expected_mandatory_count": 688,
            "fetched_count": 688,
            "extracted_count": 688,
            "provenance_valid_count": 688,
            "chunked_count": 688 if evidence_ready else 0,
        },
        "b2o_readiness": {
            "overall_readiness": {"status": "complete", "missing_or_incomplete_cell_ids": []},
            "required_provenance": {"status": "complete", "missing_or_invalid_cell_ids": []},
            "canonical_news_comparable_gate": {"status": "complete", "missing_tickers": []},
            "readiness_binding": {
                "s1_terminal_run_id": "s1",
                "s2_terminal_run_id": "s2",
                "s4_terminal_run_id": "s4",
                "inventory_id": "i" * 64,
                "universe_manifest_id": "u" * 64,
                "source_snapshot_id": "s" * 64,
                "b2o_terminal_run_id": "b2o",
            },
        },
        "convergence_plan_hash": "c" * 64,
    }


def _write_cli_inputs(tmp_path: Path) -> dict[str, Path]:
    paths = {name: tmp_path / f"{name}.json" for name in (
        "universe", "inventory", "convergence", "plan", "snapshot", "output",
    )}
    paths["universe"].write_text(
        json.dumps({
            "runtime_manifest_id": "u" * 64,
            "tickers": list(RATIFIED_TICKERS),
            "source_windows": {
                "degraded_start": "2024-01-01",
                "degraded_end": "2025-07-31",
                "canonical_start": "2025-08-01",
                "canonical_end": "2026-07-23",
            },
        }),
        encoding="utf-8",
    )
    paths["inventory"].write_text("{}", encoding="utf-8")
    paths["convergence"].write_text("{}", encoding="utf-8")
    paths["plan"].write_text(json.dumps({"plan_hash": "p" * 64}), encoding="utf-8")
    return paths


def _common_args(paths: dict[str, Path], command: str) -> list[str]:
    return [
        command,
        "--db", str(paths["db"]),
        "--universe-manifest", str(paths["universe"]),
        "--filing-inventory", str(paths["inventory"]),
        "--convergence-evidence", str(paths["convergence"]),
        "--b2o-plan", str(paths["plan"]),
        "--b2o-terminal-run-id", "b2o",
        "--baseline-snapshot-id", "s" * 64,
        "--s1-terminal-run-id", "s1",
        "--s2-terminal-run-id", "s2",
        "--s4-terminal-run-id", "s4",
    ]


@pytest.mark.parametrize("evidence_ready", [False, True])
def test_phase_gates_are_explicit_for_zero_and_complete_corpus(
    evidence_ready: bool,
) -> None:
    from catalyst_data.sec.readiness import compute_pre_b6_gate_states

    gates = compute_pre_b6_gate_states(
        b2o_source_ready=True,
        required_provenance_ready=True,
        comparable_gate_ready=True,
        sec_source_ready=True,
        sec_evidence_ready=evidence_ready,
    )

    assert gates["prebuild_source_ready"] is True
    assert gates["postbuild_evidence_ready"] is evidence_ready


def test_prebuild_audit_uses_prebuild_gate_and_reports_both_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import catalyst_data.coverage_audit as audit

    paths = _write_cli_inputs(tmp_path)
    paths["db"] = tmp_path / "candidate.db"
    monkeypatch.setattr(
        audit, "run_pre_b6_sec_readiness_audit", lambda *_, **__: _readiness_report()
    )

    assert main(_common_args(paths, "pre-b6-audit") + ["--output-dir", str(tmp_path)]) == 0

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["data"]["prebuild_source_ready"] is True
    assert envelope["data"]["postbuild_evidence_ready"] is False


def test_snapshot_is_allowed_between_the_two_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import catalyst_data.b2o as b2o

    paths = _write_cli_inputs(tmp_path)
    paths["db"] = tmp_path / "candidate.db"
    snapshot = SimpleNamespace(snapshot_id="x" * 64, plan_hash="c" * 64, to_dict=lambda: {})
    monkeypatch.setattr(b2o, "build_data_snapshot_manifest", lambda *_, **__: snapshot)
    monkeypatch.setattr(
        "catalyst_data.coverage_audit.run_pre_b6_sec_readiness_audit",
        lambda *_, **__: _readiness_report(),
    )

    argv = _common_args(paths, "pre-b6-snapshot") + [
        "--protected-source-sha256", "h" * 64,
        "--output", str(paths["snapshot"]),
    ]
    assert main(argv) == 0


def test_postbuild_promotion_gate_rejects_zero_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalyst_data.b2o import _pre_b6_gate_is_ready

    report = _readiness_report()
    assert _pre_b6_gate_is_ready(report, phase="prebuild") is True
    assert _pre_b6_gate_is_ready(report, phase="postbuild") is False


def test_phase_ready_report_binds_b2o_terminal_run_id() -> None:
    report = _readiness_report()
    assert report["b2o_readiness"]["readiness_binding"]["b2o_terminal_run_id"] == "b2o"


@pytest.mark.parametrize(
    ("inventory_update", "message"),
    [
        ({"universe_manifest_id": "x" * 64}, "universe_manifest_id"),
        ({"source_snapshot_id": "x" * 64}, "source_snapshot_id"),
    ],
)
def test_inventory_must_bind_runtime_universe_and_source_snapshot(
    inventory_update: dict[str, str], message: str
) -> None:
    from catalyst_data.sec.readiness import validate_inventory_runtime_identity

    inventory = {
        "universe_manifest_id": "u" * 64,
        "source_snapshot_id": "s" * 64,
    }
    inventory.update(inventory_update)
    with pytest.raises(Exception, match=message):
        validate_inventory_runtime_identity(
            inventory,
            universe_manifest_id="u" * 64,
            source_snapshot_id="s" * 64,
        )


def test_documented_snapshot_command_preserves_legacy_b2o_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import catalyst_data.b2o as b2o

    paths = _write_cli_inputs(tmp_path)
    paths["db"] = tmp_path / "candidate.db"
    paths["db"].touch()
    monkeypatch.setattr(
        b2o,
        "run_b2o_readiness_audit",
        lambda *_args, **_kwargs: {
            "b2o_readiness": {
                "overall_readiness": {"status": "complete"},
                "canonical_news_comparable_gate": {"status": "complete"},
            }
        },
    )
    snapshot = SimpleNamespace(snapshot_id="z" * 64, to_dict=lambda: {"snapshot_id": "z" * 64})
    monkeypatch.setattr(
        "catalyst_data.manifests.snapshot.build_legacy_data_snapshot_manifest",
        lambda *_args, **_kwargs: snapshot,
    )

    assert main([
        "snapshot",
        "--db", str(paths["db"]),
        "--universe-manifest", str(paths["universe"]),
        "--plan", str(paths["plan"]),
        "--protected-source-sha256", "h" * 64,
        "--output", str(paths["snapshot"]),
        "--terminal-run-id", "b2o",
    ]) == 0
    assert json.loads(paths["snapshot"].read_text(encoding="utf-8"))["snapshot_id"] == "z" * 64
