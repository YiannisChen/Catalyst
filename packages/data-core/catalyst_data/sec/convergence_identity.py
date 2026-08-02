"""Composite convergence_plan_hash and reconciliation_evidence_hash."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from catalyst_data.manifests.universe import sha256_identity


def compute_reconciliation_evidence_hash(
    *,
    proposed_rows: Sequence[Mapping[str, Any]],
    schema_version: str = "b2e_recon_v1",
) -> str:
    """Hash proposed/applied request_count changes; exclude operational metadata."""
    rows = []
    for r in proposed_rows:
        rows.append(
            {
                "run_id": r["run_id"],
                "cell_id": r["cell_id"],
                "logical_fetch_id": r["logical_fetch_id"],
                "endpoint_name": r.get("endpoint_name"),
                "before_request_count": r["before_request_count"],
                "after_request_count": r["after_request_count"],
            }
        )
    rows = sorted(rows, key=lambda x: (x["run_id"], x["cell_id"]))
    return sha256_identity(
        {
            "schema_version": schema_version,
            "rows": rows,
        }
    )


def compute_convergence_plan_hash(
    *,
    baseline_snapshot_id: str,
    universe_manifest_id: str,
    s1_plan_hash: str,
    s2_plan_hash: str,
    inventory_id: str,
    s4_plan_hash: str,
    reconciliation_evidence_hash: str,
    db_user_version: int = 13,
    readiness_policy_version: str = "b2e_readiness_v1",
) -> str:
    _ZERO64 = "0" * 64
    if reconciliation_evidence_hash == _ZERO64:
        raise ValueError(
            "reconciliation_evidence_hash must be computed from real FMP "
            "reconciliation data, not all-zero"
        )
    if len(reconciliation_evidence_hash) != 64:
        raise ValueError(
            f"reconciliation_evidence_hash must be 64 chars, got {len(reconciliation_evidence_hash)}"
        )
    return sha256_identity(
        {
            "schema_version": "b2e_convergence_v1",
            "baseline_snapshot_id": baseline_snapshot_id,
            "universe_manifest_id": universe_manifest_id,
            "s1_plan_hash": s1_plan_hash,
            "s2_plan_hash": s2_plan_hash,
            "inventory_id": inventory_id,
            "s4_plan_hash": s4_plan_hash,
            "reconciliation_evidence_hash": reconciliation_evidence_hash,
            "db_user_version": db_user_version,
            "readiness_policy_version": readiness_policy_version,
        }
    )
