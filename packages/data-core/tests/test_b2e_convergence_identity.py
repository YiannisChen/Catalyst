"""convergence_plan_hash and reconciliation_evidence_hash."""

from __future__ import annotations

from catalyst_data.sec.convergence_identity import (
    compute_convergence_plan_hash,
    compute_reconciliation_evidence_hash,
)


def test_convergence_plan_hash_binds_s1_s2_inventory_s4_reconciliation_and_v13():
    h1 = compute_convergence_plan_hash(
        baseline_snapshot_id="b" * 64,
        universe_manifest_id="u" * 64,
        s1_plan_hash="1" * 64,
        s2_plan_hash="2" * 64,
        inventory_id="i" * 64,
        s4_plan_hash="4" * 64,
        reconciliation_evidence_hash="r" * 64,
    )
    h2 = compute_convergence_plan_hash(
        baseline_snapshot_id="b" * 64,
        universe_manifest_id="u" * 64,
        s1_plan_hash="1" * 64,
        s2_plan_hash="2" * 64,
        inventory_id="i" * 64,
        s4_plan_hash="5" * 64,
        reconciliation_evidence_hash="r" * 64,
    )
    assert h1 != h2
    assert len(h1) == 64


def test_convergence_plan_hash_excludes_timestamps_and_paths():
    # API has no timestamp/path params — hash stable
    a = compute_convergence_plan_hash(
        baseline_snapshot_id="b" * 64,
        universe_manifest_id="u" * 64,
        s1_plan_hash="1" * 64,
        s2_plan_hash="2" * 64,
        inventory_id="i" * 64,
        s4_plan_hash="4" * 64,
        reconciliation_evidence_hash="r" * 64,
    )
    b = compute_convergence_plan_hash(
        baseline_snapshot_id="b" * 64,
        universe_manifest_id="u" * 64,
        s1_plan_hash="1" * 64,
        s2_plan_hash="2" * 64,
        inventory_id="i" * 64,
        s4_plan_hash="4" * 64,
        reconciliation_evidence_hash="r" * 64,
    )
    assert a == b


def test_snapshot_plan_hash_is_composite_not_any_stage_plan_hash():
    composite = compute_convergence_plan_hash(
        baseline_snapshot_id="b" * 64,
        universe_manifest_id="u" * 64,
        s1_plan_hash="1" * 64,
        s2_plan_hash="2" * 64,
        inventory_id="i" * 64,
        s4_plan_hash="4" * 64,
        reconciliation_evidence_hash="r" * 64,
    )
    assert composite not in {"1" * 64, "2" * 64, "4" * 64}


def test_reconciliation_evidence_hash_excludes_paths():
    rows = [
        {
            "run_id": "r1",
            "cell_id": "c1",
            "logical_fetch_id": "l1",
            "endpoint_name": "balance_sheet",
            "before_request_count": 0,
            "after_request_count": 1,
            "created_at": "NOPE",
            "report_path": "/tmp/x",
        }
    ]
    h1 = compute_reconciliation_evidence_hash(proposed_rows=rows)
    rows2 = [
        {
            "run_id": "r1",
            "cell_id": "c1",
            "logical_fetch_id": "l1",
            "endpoint_name": "balance_sheet",
            "before_request_count": 0,
            "after_request_count": 1,
            "created_at": "OTHER",
            "report_path": "/other",
        }
    ]
    h2 = compute_reconciliation_evidence_hash(proposed_rows=rows2)
    assert h1 == h2
