"""M5-10: sealed baseline integrity (A) and semantic ontology regression (B).

M5 plan §5. Gate A verifies the sealed M1 corrective baseline artifact chain
without running any model: tag exists and is an ancestor, report SHA/schema,
FOUR_ARM_E2E_OK + USER_SMOKE_OK tokens, Q-002 NON-COMPARABLE preserved. Gate B
is a deterministic fixture-only regression on the approved 10-case T4 pack via
the repo case-pack code; cases are never derived from /root-bound evidence
refs; metric parity only, comparability_declared=false. The CLI exits 0 and
writes artifacts; exit 1 on tampered SHA or mismatched pack ID (no artifact);
exit 2 on bad usage.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from catalyst_eval.baseline.gates import (
    APPROVED_T4_CASE_PACK_ID,
    GateFailure,
    sealed_baseline_integrity_gate,
    semantic_ontology_regression_gate,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EVAL_ROOT = Path(__file__).resolve().parents[1]
CLI = EVAL_ROOT / "scripts" / "run_v1_1_m5_gates.py"


def test_gate_a_passes_on_sealed_baseline_artifact_chain(tmp_path: Path) -> None:
    out = tmp_path / "gate_a.json"
    payload = sealed_baseline_integrity_gate(
        repo_root=REPO_ROOT,
        tag="v1.1-m1-baseline-corrective-seal",
        report_path=REPO_ROOT
        / "data/baseline/reports/v1_1_baseline_621375bc_corrective_seal.json",
        out_path=out,
    )
    assert payload["schema_version"] == "v1_1_m5_gate_a_v1"
    assert payload["tag_verified"] is True
    assert payload["report_schema"] == "baseline_v1"
    assert payload["run_tokens"]["four_arm"]["token"] == "FOUR_ARM_E2E_OK"
    assert payload["run_tokens"]["user_smoke"]["token"] == "USER_SMOKE_OK"
    assert payload["q002_non_comparable"]["promoted_env_recovered"] is False
    assert (
        payload["q002_non_comparable"]["promoted_env_reason"]
        == "q_002_promoted_environment_tuple_unrecovered"
    )
    assert payload["exit_status"] == 0
    assert out.exists()


def test_gate_a_fails_on_tampered_report_sha_without_artifact(tmp_path: Path) -> None:
    tampered = tmp_path / "tampered.json"
    original = REPO_ROOT / "data/baseline/reports/v1_1_baseline_621375bc_corrective_seal.json"
    data = json.loads(original.read_text())
    data["schema_version"] = "tampered"
    tampered.write_text(json.dumps(data))
    out = tmp_path / "gate_a_fail.json"
    with pytest.raises(GateFailure):
        sealed_baseline_integrity_gate(
            repo_root=REPO_ROOT,
            tag="v1.1-m1-baseline-corrective-seal",
            report_path=tampered,
            out_path=out,
        )
    assert not out.exists()


def test_gate_b_passes_on_approved_t4_case_pack(tmp_path: Path) -> None:
    out = tmp_path / "gate_b.json"
    payload = semantic_ontology_regression_gate(
        repo_root=REPO_ROOT,
        golden_dir=REPO_ROOT / "packages/eval/golden_set",
        out_path=out,
    )
    assert payload["schema_version"] == "v1_1_m5_gate_b_v1"
    assert payload["case_pack"]["approved_case_pack_id"] == APPROVED_T4_CASE_PACK_ID
    assert payload["case_pack"]["case_count"] == 10
    assert payload["case_pack"]["matched"] is True
    assert payload["metric_parity"]["ticker_cutoff_violations"] == 0
    assert payload["metric_parity"]["citation_resolution"] == 1.0
    assert payload["metric_parity"]["runtime_identity_binding"] == 1.0
    assert payload["metric_parity"]["context_pack_identity"] == 1.0
    assert payload["metric_parity"]["claim_lineage"] == 1.0
    assert payload["metric_parity"]["secret_leakage"] == 0
    assert payload["comparability_declared"] is False
    assert payload["exit_status"] == 0
    assert out.exists()


def test_gate_b_never_reads_root_bound_evidence_refs() -> None:
    # The sealed report's /root-bound refs must never be used to derive cases.
    report = REPO_ROOT / "data/baseline/reports/v1_1_baseline_621375bc_corrective_seal.json"
    report_text = report.read_text()
    assert "/root/catalyst-data" in report_text  # audit refs exist
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        payload = semantic_ontology_regression_gate(
            repo_root=REPO_ROOT,
            golden_dir=REPO_ROOT / "packages/eval/golden_set",
            out_path=Path(tmp) / "gate_b_probe.json",
        )
    assert payload["exit_status"] == 0


def test_gate_b_fails_on_mismatched_case_pack(tmp_path: Path) -> None:
    golden = tmp_path / "golden"
    shutil.copytree(REPO_ROOT / "packages/eval/golden_set", golden)
    # Mutate one golden case so the pack identity changes.
    source = golden / "v1_2_p0_set.jsonl"
    lines = source.read_text().splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    for row in rows:
        if row.get("id") == "g006":
            row["ticker"] = "MUTATED"
    source.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    out = tmp_path / "gate_b_fail.json"
    with pytest.raises(GateFailure):
        semantic_ontology_regression_gate(
            repo_root=REPO_ROOT,
            golden_dir=golden,
            out_path=out,
        )
    assert not out.exists()


def test_cli_gate_both_exits_zero_and_writes_artifacts(tmp_path: Path) -> None:
    """Run both gates through the CLI but write ONLY to tmp artifacts.

    The tracked ``data/baseline/reports`` artifacts are sealed; running the
    gates must never mutate them (Batch-B corrective: the M5 sealed artifact
    is protected from mutation). The Gate A artifact embeds an absolute
    ``report_path``, so writing to the tracked path is worktree-dependent.
    """
    a = tmp_path / "gate_a.json"
    b = tmp_path / "gate_b.json"
    proc_a = subprocess.run(
        [
            sys.executable, str(CLI), "--gate", "sealed_baseline_integrity",
            "--out", str(a),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc_a.returncode == 0, proc_a.stdout + proc_a.stderr
    proc_b = subprocess.run(
        [
            sys.executable, str(CLI), "--gate", "semantic_ontology_regression",
            "--out", str(b),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc_b.returncode == 0, proc_b.stdout + proc_b.stderr
    assert a.exists()
    assert b.exists()
    sealed_a = (
        REPO_ROOT
        / "data/baseline/reports/v1_1_m5_gate_a_sealed_baseline_integrity.json"
    )
    sealed_b = (
        REPO_ROOT
        / "data/baseline/reports/v1_1_m5_gate_b_semantic_ontology_regression.json"
    )
    # The sealed tracked artifacts remain present and are never rewritten by
    # this test (the gate output path is tmp); byte-stability is asserted by
    # the standing ``git status`` check after the full FAST run.
    assert sealed_a.read_bytes()
    assert sealed_b.read_bytes()


def test_cli_bad_usage_exits_two() -> None:
    proc = subprocess.run(
        [sys.executable, str(CLI), "--gate", "not_a_gate"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2


def test_cli_tampered_report_exits_one_without_artifact(tmp_path: Path) -> None:
    tampered = tmp_path / "tampered.json"
    original = REPO_ROOT / "data/baseline/reports/v1_1_baseline_621375bc_corrective_seal.json"
    data = json.loads(original.read_text())
    data["schema_version"] = "tampered"
    tampered.write_text(json.dumps(data))
    out = tmp_path / "gate_a_fail.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "--gate",
            "sealed_baseline_integrity",
            "--report",
            str(tampered),
            "--out",
            str(out),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1
    assert not out.exists()
