"""Source-agnostic checkpoint request_count reconciliation (candidate only)."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from catalyst_data.storage.sqlite import FROZEN_PATHS, _assert_not_frozen


@dataclass(frozen=True)
class ProposedChange:
    run_id: str
    cell_id: str
    logical_fetch_id: str
    endpoint_name: str | None
    before_request_count: int
    after_request_count: int
    checkpoint_id: str | None = None


@dataclass(frozen=True)
class ProposedCheckpointChanges:
    changes: tuple[ProposedChange, ...]
    plan_hash: str


def _candidate_guard(db_path: str | Path) -> Path:
    path = Path(db_path).resolve()
    real = os.path.realpath(str(path))
    _assert_not_frozen(real)
    posix = Path(real).as_posix()
    if "data/snapshots" in posix:
        raise PermissionError(f"refusing snapshot path: {real}")
    # Reject active promoted pointer (symlink escape)
    if path.is_symlink() or Path(db_path).is_symlink():
        raise PermissionError(f"refusing symlink path: {db_path}")
    if "data/candidates" not in posix:
        raise PermissionError(f"only data/candidates paths allowed: {real}")
    if real in FROZEN_PATHS:
        raise PermissionError(f"frozen path: {real}")
    return path


def _plan_hash_for_changes(changes: Sequence[ProposedChange]) -> str:
    from catalyst_data.manifests.universe import sha256_identity

    return sha256_identity(
        {
            "schema_version": "b2e_recon_v1",
            "changes": [
                {
                    "checkpoint_id": c.checkpoint_id,
                    "run_id": c.run_id,
                    "cell_id": c.cell_id,
                    "logical_fetch_id": c.logical_fetch_id,
                    "endpoint_name": c.endpoint_name,
                    "before_request_count": c.before_request_count,
                    "after_request_count": c.after_request_count,
                }
                for c in changes
            ],
        }
    )


def plan_checkpoint_reconciliation(
    db_path: str | Path,
    *,
    lineage_run_ids: Sequence[str],
    source_type: str | None = None,
    endpoint_name: str | None = None,
) -> ProposedCheckpointChanges:
    if not lineage_run_ids:
        raise ValueError("lineage_run_ids must be non-empty")
    path = _candidate_guard(db_path)
    conn = sqlite3.connect(str(path))
    try:
        placeholders = ",".join("?" for _ in lineage_run_ids)
        q = f"""
            SELECT checkpoint_id, run_id, cell_id, logical_fetch_id, endpoint_name,
                   request_count, raw_asset_id
            FROM source_checkpoints
            WHERE run_id IN ({placeholders})
              AND cell_id IS NOT NULL
              AND logical_fetch_id IS NOT NULL
        """
        params: list[Any] = list(lineage_run_ids)
        if source_type:
            q += " AND source_type = ?"
            params.append(source_type)
        if endpoint_name:
            q += " AND endpoint_name = ?"
            params.append(endpoint_name)
        changes: list[ProposedChange] = []
        for row in conn.execute(q, params):
            ck_id, run_id, cell_id, lfid, ep, req, raw_asset_id = row
            # Eligibility: SUCCEEDED attempt matching logical_fetch_id, endpoint,
            # run lineage, and raw_asset.request_id → that succeeded attempt.
            succ = conn.execute(
                """SELECT request_id, raw_asset_id, endpoint_name, run_id, status
                   FROM provider_request_attempts
                   WHERE logical_fetch_id=? AND status='SUCCEEDED'
                   ORDER BY attempt_no DESC""",
                (lfid,),
            ).fetchall()
            if not succ:
                continue
            eligible = False
            for request_id, att_raw, att_ep, att_run, _st in succ:
                if att_run not in set(lineage_run_ids):
                    continue
                if ep is not None and att_ep is not None and att_ep != ep:
                    continue
                if raw_asset_id and att_raw and raw_asset_id != att_raw:
                    continue
                if raw_asset_id:
                    ra_cols = {
                        r[1]
                        for r in conn.execute("PRAGMA table_info(raw_assets)").fetchall()
                    }
                    if "asset_id" not in ra_cols:
                        continue
                    if "request_id" in ra_cols:
                        ra = conn.execute(
                            "SELECT request_id FROM raw_assets WHERE asset_id=?",
                            (raw_asset_id,),
                        ).fetchone()
                        if ra is None:
                            continue
                        if ra[0] and ra[0] != request_id:
                            continue
                    else:
                        ra = conn.execute(
                            "SELECT 1 FROM raw_assets WHERE asset_id=?",
                            (raw_asset_id,),
                        ).fetchone()
                        if ra is None:
                            continue
                eligible = True
                break
            if not eligible:
                continue
            cnt = _count_attempts(
                conn,
                run_id=run_id,
                logical_fetch_id=lfid,
                endpoint_name=ep,
            )
            if int(req or 0) == int(cnt):
                continue
            changes.append(
                ProposedChange(
                    run_id=run_id,
                    cell_id=cell_id,
                    logical_fetch_id=lfid,
                    endpoint_name=ep,
                    before_request_count=int(req or 0),
                    after_request_count=int(cnt),
                    checkpoint_id=ck_id,
                )
            )
        plan_hash = _plan_hash_for_changes(changes)
        return ProposedCheckpointChanges(changes=tuple(changes), plan_hash=plan_hash)
    finally:
        conn.close()


def _count_attempts(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    logical_fetch_id: str,
    endpoint_name: str | None,
) -> int:
    """Count attempts bound to run + logical_fetch_id + endpoint (not lfid alone)."""
    q = """
        SELECT COUNT(*) FROM provider_request_attempts
        WHERE run_id=? AND logical_fetch_id=?
    """
    params: list[Any] = [run_id, logical_fetch_id]
    if endpoint_name is not None:
        q += " AND endpoint_name=?"
        params.append(endpoint_name)
    return int(conn.execute(q, params).fetchone()[0])


def _eligible_succeeded_exists(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    logical_fetch_id: str,
    endpoint_name: str | None,
    raw_asset_id: str | None,
) -> bool:
    rows = conn.execute(
        """SELECT request_id, raw_asset_id, endpoint_name, run_id, status
           FROM provider_request_attempts
           WHERE logical_fetch_id=? AND status='SUCCEEDED'
           ORDER BY attempt_no DESC""",
        (logical_fetch_id,),
    ).fetchall()
    for request_id, att_raw, att_ep, att_run, _st in rows:
        if att_run != run_id:
            continue
        if endpoint_name is not None and att_ep is not None and att_ep != endpoint_name:
            continue
        if raw_asset_id and att_raw and raw_asset_id != att_raw:
            continue
        ra_cols = {r[1] for r in conn.execute("PRAGMA table_info(raw_assets)").fetchall()}
        if raw_asset_id and "request_id" in ra_cols:
            ra = conn.execute(
                "SELECT request_id FROM raw_assets WHERE asset_id=?",
                (raw_asset_id,),
            ).fetchone()
            if ra is None:
                continue
            if ra[0] and ra[0] != request_id:
                continue
        elif raw_asset_id:
            if (
                conn.execute(
                    "SELECT 1 FROM raw_assets WHERE asset_id=?", (raw_asset_id,)
                ).fetchone()
                is None
            ):
                continue
        return True
    return False


def apply_checkpoint_reconciliation(
    db_path: str | Path,
    plan: ProposedCheckpointChanges,
    *,
    expected_plan_hash: str,
    dry_run: bool = True,
    audit_path: str | Path | None = None,
) -> ProposedCheckpointChanges:
    if plan.plan_hash != expected_plan_hash:
        raise ValueError("expected_plan_hash mismatch")
    path = _candidate_guard(db_path)
    if dry_run:
        return plan
    lineage = list({c.run_id for c in plan.changes})
    if not lineage and plan.changes:
        raise ValueError("plan changes missing run_id")
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        applied: list[dict[str, Any]] = []
        for ch in plan.changes:
            # Apply-time revalidation inside BEGIN IMMEDIATE
            row = conn.execute(
                """SELECT request_count, logical_fetch_id, endpoint_name, checkpoint_id,
                          raw_asset_id
                   FROM source_checkpoints
                   WHERE run_id=? AND cell_id=?""",
                (ch.run_id, ch.cell_id),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise RuntimeError(f"checkpoint missing for cell_id={ch.cell_id}")
            cur_req, cur_lfid, cur_ep, cur_ck, cur_raw = row
            if cur_lfid != ch.logical_fetch_id:
                conn.execute("ROLLBACK")
                raise RuntimeError(f"logical_fetch_id drift for cell_id={ch.cell_id}")
            if ch.endpoint_name is not None and cur_ep != ch.endpoint_name:
                conn.execute("ROLLBACK")
                raise RuntimeError(f"endpoint_name drift for cell_id={ch.cell_id}")
            if not _eligible_succeeded_exists(
                conn,
                run_id=ch.run_id,
                logical_fetch_id=ch.logical_fetch_id,
                endpoint_name=ch.endpoint_name,
                raw_asset_id=cur_raw,
            ):
                conn.execute("ROLLBACK")
                raise RuntimeError(
                    f"eligible SUCCEEDED attempt missing for cell_id={ch.cell_id}"
                )
            live_count = _count_attempts(
                conn,
                run_id=ch.run_id,
                logical_fetch_id=ch.logical_fetch_id,
                endpoint_name=ch.endpoint_name,
            )
            if live_count != int(ch.after_request_count):
                conn.execute("ROLLBACK")
                raise RuntimeError(
                    f"ledger changed after plan for cell_id={ch.cell_id}: "
                    f"live={live_count} plan_after={ch.after_request_count}; replan required"
                )
            if int(cur_req or 0) == int(ch.after_request_count):
                applied.append(
                    {
                        "run_id": ch.run_id,
                        "cell_id": ch.cell_id,
                        "before_request_count": ch.before_request_count,
                        "after_request_count": ch.after_request_count,
                        "noop": True,
                    }
                )
                continue
            if int(cur_req or 0) != int(ch.before_request_count):
                conn.execute("ROLLBACK")
                raise RuntimeError(
                    f"request_count drift for cell_id={ch.cell_id}: "
                    f"db={cur_req} plan_before={ch.before_request_count}"
                )
            cur = conn.execute(
                """UPDATE source_checkpoints SET request_count=?
                   WHERE run_id=? AND cell_id=? AND logical_fetch_id=?
                     AND request_count=?""",
                (
                    ch.after_request_count,
                    ch.run_id,
                    ch.cell_id,
                    ch.logical_fetch_id,
                    ch.before_request_count,
                ),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                raise RuntimeError(
                    f"rowcount mismatch for cell_id={ch.cell_id}: {cur.rowcount}"
                )
            applied.append(
                {
                    "run_id": ch.run_id,
                    "cell_id": ch.cell_id,
                    "before_request_count": ch.before_request_count,
                    "after_request_count": ch.after_request_count,
                    "noop": False,
                }
            )
        # Write audit before commit so audit failure can roll back DB
        if audit_path:
            p = Path(audit_path)
            if p.parent and not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "plan_hash": plan.plan_hash,
                "field": "request_count",
                "changes": [
                    {
                        "run_id": c.run_id,
                        "cell_id": c.cell_id,
                        "logical_fetch_id": c.logical_fetch_id,
                        "endpoint_name": c.endpoint_name,
                        "checkpoint_id": c.checkpoint_id,
                        "before_request_count": c.before_request_count,
                        "after_request_count": c.after_request_count,
                        "before_field_hash": _field_hash(c.before_request_count),
                        "after_field_hash": _field_hash(c.after_request_count),
                    }
                    for c in plan.changes
                ],
                "applied": applied,
            }
            tmp = p.with_suffix(p.suffix + ".tmp")
            try:
                tmp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
                os.replace(tmp, p)
                audit_written = True
            except Exception:
                conn.execute("ROLLBACK")
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                raise
        conn.execute("COMMIT")
        return plan
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _field_hash(value: Any) -> str:
    from catalyst_data.manifests.universe import sha256_identity

    return sha256_identity({"field": "request_count", "value": value})
