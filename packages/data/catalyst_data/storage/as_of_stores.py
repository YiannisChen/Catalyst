"""Read-only ``as_of`` facade over manifest-verified SQLite snapshots."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

from catalyst_data.ingestion import input_manifest
from catalyst_data.storage import legacy_cohorts, snapshots, temporal_evidence
from catalyst_data.storage.snapshots import VerifiedStores
from catalyst_data.storage.time_corrections import (
    CorrectionTable,
    load_corrections,
)


def _verified_corrections(
    verified: VerifiedStores,
    tables: tuple[CorrectionTable, ...],
    artifacts: Mapping[str, str | Path],
) -> None:
    table_ids = {table.table_id for table in tables}
    if set(artifacts) != table_ids:
        raise ValueError("each correction table must name its pinned input artifact")
    paths = {}
    for table in tables:
        raw_path = artifacts[table.table_id]
        path = Path(raw_path).expanduser()
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("correction artifacts must be absolute pinned files")
        paths[table.table_id] = path.resolve(strict=True)

    if not tables:
        return
    receipt = input_manifest.verify(
        verified.manifest_path,
        invocation=list(verified.invocation),
        required_paths=tuple(paths.values()),
        required_env=verified.required_env,
        environ=verified.environ_override,
    )
    if receipt["manifest_sha256"] != verified.manifest_sha256:
        raise input_manifest.PreflightError("input manifest changed after store verification")
    pins = {Path(row["path"]): row["sha256"] for row in receipt["files"]}
    for table in tables:
        path = paths[table.table_id]
        digest = pins.get(path)
        if digest != table.sha256 or input_manifest.fingerprint(path) != table.sha256:
            raise input_manifest.PreflightError("correction table bytes do not match their pinned identity")
        loaded = load_corrections(
            path.read_bytes(), kind=table.kind, filename=path.name,
            expected_sha256=table.sha256,
        )
        if loaded != table:
            raise ValueError("correction table object differs from its pinned CSV bytes")


def as_of_stores(
    verified: VerifiedStores,
    ticker: str,
    window_start: datetime,
    window_end: datetime,
    at: datetime | None,
    *,
    mode: str,
    correction_table_id: str,
    store_ids: tuple[str, ...] | list[str] | None = None,
    correction_tables: Iterable[CorrectionTable] = (),
    correction_store_ids: Mapping[str, str] | None = None,
    correction_artifacts: Mapping[str, str | Path] | None = None,
    kinds: list[str] | tuple[str, ...] | None = None,
    allowed_legacy_cohorts: list[str] | tuple[str, ...] = (),
    legacy_catalogs: Mapping[str, str | Path] | None = None,
    contract_version: str = "data-core-v1.0",
    dedup_version: str = "same-article-release-v1",
) -> dict:
    """Query only the required and present optional stores in a verified set.

    Snapshot identity always comes from the input manifest. Correction CSV
    bytes must be separately pinned in that same manifest, and their source
    scope is bound explicitly by logical store ID in ``correction_store_ids``.
    Repeated version IDs in distinct stores remain distinct physical evidence;
    conflicting duplicate identities within one store fail closed.
    """
    snapshots.validate_verified_stores(verified)
    catalog_paths = {} if legacy_catalogs is None else dict(legacy_catalogs)
    if set(allowed_legacy_cohorts) != set(catalog_paths):
        raise ValueError("each requested legacy cohort requires its pinned catalog")
    catalogs = []
    for cohort_id, path in catalog_paths.items():
        cohort = legacy_cohorts.load_cohort(path, verified)
        if cohort.cohort_id != cohort_id:
            raise ValueError("legacy catalog identity does not match requested cohort")
        catalogs.append(cohort)
    tables = tuple(correction_tables)
    if any(not isinstance(table, CorrectionTable) for table in tables):
        raise ValueError("correction_tables must contain validated CorrectionTable objects")
    if len({table.table_id for table in tables}) != len(tables):
        raise ValueError("duplicate correction table identity")
    artifacts = {} if correction_artifacts is None else dict(correction_artifacts)
    _verified_corrections(verified, tables, artifacts)

    if store_ids is None:
        selected_ids = set(verified.stores)
    else:
        if (not isinstance(store_ids, (tuple, list)) or not store_ids
                or any(not isinstance(item, str) for item in store_ids)
                or len(store_ids) != len(set(store_ids))
                or not set(store_ids) <= set(verified.stores)):
            raise ValueError("store_ids must select unique present verified stores")
        selected_ids = set(store_ids)
    bindings = {} if correction_store_ids is None else dict(correction_store_ids)
    if any(store_id not in selected_ids for store_id in bindings):
        raise ValueError("correction store binding references an undeclared or missing store")
    if any(not isinstance(scope, str) or not scope.strip() or scope != scope.strip()
           for scope in bindings.values()):
        raise ValueError("correction store bindings must name exact CSV store scopes")

    evidence: list[dict] = []
    leads: list[dict] = []
    identities: set[tuple[str, str]] = set()
    per_store_audits: dict[str, dict] = {}
    audit_seed: dict | None = None
    snapshot_ids = {store_id: verified.stores[store_id].sha256
                    for store_id in sorted(selected_ids)}
    try:
        for store_id in sorted(selected_ids):
            store = verified.stores[store_id]
            with snapshots.readonly_snapshot(verified, store) as conn:
                store_bindings = ([(table, bindings[store_id]) for table in tables]
                                  if store_id in bindings else [])
                result = temporal_evidence.as_of(
                    conn, ticker, window_start, window_end, at,
                    mode=mode, kinds=kinds,
                    allowed_legacy_cohorts=allowed_legacy_cohorts,
                    legacy_verified_stores=verified,
                    legacy_catalogs=catalog_paths,
                    correction_tables=store_bindings,
                    data_snapshot_id=store.sha256,
                    correction_table_id=correction_table_id,
                    contract_version=contract_version,
                    dedup_version=dedup_version,
                )
            if audit_seed is None:
                audit_seed = dict(result["audit"])
            per_store_audits[store_id] = {
                "data_snapshot_id": store.sha256,
                "correction_store_id": bindings.get(store_id),
                "correction_conflict_count": result["audit"]["correction_conflict_count"],
                "correction_conflict_count_scope": "ticker_kind_candidates_before_window_filter",
                "provenance_counts": dict(result["audit"]["provenance_counts"]),
                "evidence_count": len(result["evidence"]),
                "lead_count": len(result["leads"]),
            }
            for destination, rows in ((evidence, result["evidence"]), (leads, result["leads"])):
                for row in rows:
                    identity = (store_id, row["version_id"])
                    if identity in identities:
                        raise ValueError("duplicate version identity within a store snapshot")
                    identities.add(identity)
                    destination.append({**row, "store_id": store_id,
                                        "snapshot_id": store.sha256})
    except sqlite3.Error as exc:
        raise ValueError("store snapshot is unreadable or has an invalid temporal schema") from exc

    key = lambda row: (row["eligible_at"] or "", row["store_id"], row["source_id"],
                       row["document_id"], row["version_id"])
    evidence.sort(key=key)
    leads.sort(key=key)
    counts: dict[str, int] = {}
    for row in evidence + leads:
        counts[row["provenance"]] = counts.get(row["provenance"], 0) + 1
    audit = audit_seed or {}
    audit.update({
        "data_snapshot_id": None,
        "data_snapshot_ids": snapshot_ids,
        "input_snapshot_ids": {key: pin.sha256 for key, pin in sorted(verified.stores.items())},
        "manifest_sha256": verified.manifest_sha256,
        "legacy_catalogs": {cohort.cohort_id: {
            "catalog_sha256": cohort.manifest_sha256,
            "membership_sha256": cohort.membership_sha256,
            "source_snapshot_sha256": cohort.source_snapshot_sha256,
        } for cohort in catalogs},
        "missing_optional_store_ids": list(verified.missing_optional_store_ids),
        "correction_table_ids": sorted(table.table_id for table in tables),
        "correction_table_id": correction_table_id,
        "correction_store_ids": {store_id: bindings[store_id] for store_id in sorted(bindings)},
        "correction_conflict_count": sum(
            item["correction_conflict_count"] for item in per_store_audits.values()
        ),
        "correction_conflict_count_scope": "ticker_kind_candidates_before_window_filter",
        "per_store_audits": per_store_audits,
        "provenance_counts": counts,
        "evidence_count": len(evidence),
        "lead_count": len(leads),
        "store_count": len(selected_ids),
    })
    return {"evidence": evidence, "leads": leads, "audit": audit}


__all__ = ["as_of_stores"]
