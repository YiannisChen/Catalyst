"""Read-only composition of verified body, metadata, and coverage views."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from catalyst_data.ingestion import input_manifest
from catalyst_data.ingestion.coverage_readiness import CoveragePlan, CoverageRequirement
from catalyst_data.ingestion.gap_readiness import evaluate_snapshots
from catalyst_data.ingestion.time import timefields
from catalyst_data.retrieval.evidence_search import search_stores
from catalyst_data.storage.as_of_stores import as_of_stores
from catalyst_data.storage.read_adapters import read_store_facts
from catalyst_data.storage.snapshots import (
    VerifiedStores,
    validate_verified_stores,
)
from catalyst_data.storage.time_corrections import CorrectionTable


def _verify_all_inputs(verified: VerifiedStores) -> dict:
    validate_verified_stores(verified)
    receipt = input_manifest.verify(
        verified.manifest_path,
        invocation=list(verified.invocation),
        required_env=verified.required_env,
        environ=verified.environ_override,
    )
    if receipt["manifest_sha256"] != verified.manifest_sha256:
        raise input_manifest.PreflightError("input manifest changed after store verification")
    return receipt


def _store_ids(value: Sequence[str], verified: VerifiedStores) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or any(not isinstance(item, str) for item in value):
        raise ValueError("body_store_ids must be an explicit sequence of store IDs")
    normalized = tuple(value)
    if (len(normalized) != len(set(normalized))
            or not set(normalized) <= set(verified.stores)):
        raise ValueError("body_store_ids must uniquely select verified stores")
    return tuple(sorted(normalized))


def _metadata_stores(value, verified: VerifiedStores) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError("metadata_stores must explicitly list (store_id, adapter) pairs")
    pairs = tuple(value)
    if any(not isinstance(pair, (tuple, list)) or len(pair) != 2 for pair in pairs):
        raise ValueError("metadata_stores entries must be (store_id, adapter) pairs")
    normalized = tuple((pair[0], pair[1]) for pair in pairs)
    if (any(not isinstance(store_id, str) or not isinstance(adapter, str)
            for store_id, adapter in normalized)
            or len({store_id for store_id, _adapter in normalized}) != len(normalized)
            or any(store_id not in verified.stores for store_id, _adapter in normalized)):
        raise ValueError("metadata_stores must use unique verified store IDs and adapters")
    return tuple(sorted(normalized))


def _query_time(value: datetime, name: str) -> tuple[datetime, str]:
    try:
        parsed = timefields.stored(value).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a timezone-aware timestamp") from None
    return parsed, parsed.isoformat(timespec="microseconds")


def query_stores(
    *,
    verified_stores: VerifiedStores,
    body_store_ids: Sequence[str],
    metadata_stores: Sequence[tuple[str, str]],
    registry_path: str | Path | None,
    ticker: str,
    query: str,
    window_start: datetime,
    window_end: datetime,
    cutoff: datetime,
    mode: str,
    correction_table_id: str,
    correction_tables: Sequence[CorrectionTable],
    correction_store_ids: Mapping[str, str],
    correction_artifacts: Mapping[str, str | Path],
    requirements: Sequence[CoverageRequirement],
    plan: CoveragePlan,
    allowed_legacy_cohorts: Sequence[str],
    legacy_catalogs: Mapping[str, str | Path],
    market_sessions: Mapping[str, Sequence[str]] | None = None,
    market_source_ids: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Combine pinned historical evidence, metadata leads, facts, and readiness.

    Body and metadata items retain their source store and identity fields when
    placed in separate result collections. Coverage readiness is evaluated
    solely against caller-declared requirements and the supplied current plan;
    positive evidence never implies complete coverage. The audit records
    versioned hashes of the requirements and plan, but not their full contents.
    Callers must retain the original declarations or a complete canonical
    declaration file to reproduce those hashes; hashes alone cannot reconstruct
    their inputs.
    """
    if not isinstance(verified_stores, VerifiedStores):
        raise ValueError("manifest-verified stores are required")
    _verify_all_inputs(verified_stores)
    if mode not in {"observed", "published"}:
        raise ValueError("mode must be observed or published")
    window_start, audit_window_start = _query_time(window_start, "window_start")
    window_end, audit_window_end = _query_time(window_end, "window_end")
    cutoff, audit_cutoff = _query_time(cutoff, "cutoff")
    if window_start >= window_end:
        raise ValueError("query window must be a nonempty increasing interval")
    body_ids = _store_ids(body_store_ids, verified_stores)
    metadata_pairs = _metadata_stores(metadata_stores, verified_stores)
    metadata_ids = {store_id for store_id, _adapter in metadata_pairs}
    market_pairs = {store_id: adapter for store_id, adapter in metadata_pairs
                    if adapter in {"daily_bars", "minute_bars"}}
    if market_sessions is None:
        declared_market_sessions = {}
    elif isinstance(market_sessions, Mapping):
        declared_market_sessions = dict(market_sessions)
    else:
        raise ValueError("market_sessions must map selected bar store IDs to explicit sessions")
    if set(declared_market_sessions) != set(market_pairs):
        raise ValueError("market_sessions must exactly match selected bar stores")
    if market_source_ids is None:
        declared_market_source_ids = {}
    elif isinstance(market_source_ids, Mapping):
        declared_market_source_ids = dict(market_source_ids)
    else:
        raise ValueError("market_source_ids must map selected bar store IDs to registered sources")
    if set(declared_market_source_ids) != set(market_pairs):
        raise ValueError("market_source_ids must exactly match selected bar stores")
    if any(not isinstance(source_id, str) or not source_id.strip()
           or source_id != source_id.strip()
           for source_id in declared_market_source_ids.values()):
        raise ValueError("market_source_ids must contain nonempty registered source IDs")
    normalized_market_sessions = {}
    for store_id, sessions in declared_market_sessions.items():
        if (not isinstance(sessions, (tuple, list)) or not sessions
                or any(not isinstance(session, str) for session in sessions)
                or len(sessions) != len(set(sessions))):
            raise ValueError("market sessions must be nonempty unique explicit date sequences")
        normalized_market_sessions[store_id] = tuple(sorted(sessions))
    if (not isinstance(ticker, str) or not ticker.strip() or ticker != ticker.strip()
            or not isinstance(query, str) or not query.strip()):
        raise ValueError("ticker and query must be nonempty trimmed values")
    canonical_ticker = ticker.upper()
    requirement_rows = tuple(requirements)
    if not requirement_rows or any(not isinstance(item, CoverageRequirement) for item in requirement_rows):
        raise ValueError("validated coverage requirements are required")
    if any(item.ticker.upper() != canonical_ticker for item in requirement_rows):
        raise ValueError("query ticker conflicts with a coverage requirement")
    if not isinstance(plan, CoveragePlan):
        raise ValueError("an explicit CoveragePlan is required")
    if not isinstance(correction_table_id, str) or not correction_table_id.strip():
        raise ValueError("correction_table_id must be explicit")
    tables = tuple(correction_tables)
    if any(not isinstance(table, CorrectionTable) for table in tables):
        raise ValueError("correction_tables must contain validated CorrectionTable values")
    if len({table.table_id for table in tables}) != len(tables):
        raise ValueError("duplicate correction table identity")
    if not isinstance(correction_store_ids, Mapping) or not isinstance(correction_artifacts, Mapping):
        raise ValueError("correction store bindings and artifact paths must be mappings")
    bindings = dict(correction_store_ids)
    artifacts = dict(correction_artifacts)
    if set(bindings) - (set(body_ids) | metadata_ids):
        raise ValueError("correction binding references an undeclared query store")
    if bindings and not tables:
        raise ValueError("correction bindings require validated correction tables")
    if set(bindings).intersection(market_pairs):
        raise ValueError("market bar stores do not support publication-time corrections")
    if set(artifacts) != {table.table_id for table in tables}:
        raise ValueError("correction artifacts must exactly match supplied correction tables")
    for table in tables:
        if not isinstance(artifacts[table.table_id], (str, Path)):
            raise ValueError("correction artifact paths must be explicit")
    selected_metadata_correction = (
        correction_table_id if metadata_ids.intersection(bindings) else None
    )
    if selected_metadata_correction is not None and selected_metadata_correction not in {
        table.table_id for table in tables
    }:
        raise ValueError("metadata correction binding requires the selected correction table")
    body_table_ids = set(body_ids).intersection(bindings)
    if body_table_ids and correction_table_id not in {table.table_id for table in tables}:
        raise ValueError("body correction table ID must match a supplied and pinned correction table")
    consumed_correction_ids = set()
    if body_table_ids:
        consumed_correction_ids.add(correction_table_id)
    if selected_metadata_correction is not None:
        consumed_correction_ids.add(selected_metadata_correction)
    if {table.table_id for table in tables} != consumed_correction_ids:
        raise ValueError("correction tables or artifacts are unbound or unused")
    if set(bindings) - (set(body_ids) | metadata_ids):
        raise ValueError("correction binding references an unselected store")
    legacy_ids = tuple(allowed_legacy_cohorts)
    legacy_paths = dict(legacy_catalogs)
    if (len(set(legacy_ids)) != len(legacy_ids)
            or set(legacy_paths) != set(legacy_ids)):
        raise ValueError("legacy catalogs must exactly match the explicit allowlist")
    if legacy_ids and not body_ids:
        raise ValueError("legacy catalogs require selected body stores")
    if registry_path is None and metadata_pairs:
        raise ValueError("metadata reads require an explicit pinned source registry")
    if registry_path is not None and not metadata_pairs:
        raise ValueError("registry_path is only accepted with explicit metadata stores")

    body_tables = ((next(table for table in tables if table.table_id == correction_table_id),)
                   if body_table_ids else ())
    body_artifacts = ({correction_table_id: artifacts[correction_table_id]}
                      if body_tables else {})
    body_bindings = {key: value for key, value in bindings.items() if key in body_ids}
    evidence: list[dict[str, Any]] = []
    body_leads: list[dict[str, Any]] = []
    body_read_audit: dict[str, Any] | None = None
    search_audit: dict[str, Any] | None = None

    if body_ids:
        body_read = as_of_stores(
            verified_stores, canonical_ticker, window_start, window_end, cutoff,
            mode=mode, correction_table_id=correction_table_id, store_ids=body_ids,
            correction_tables=body_tables, correction_store_ids=body_bindings,
            correction_artifacts=body_artifacts,
            allowed_legacy_cohorts=legacy_ids,
            legacy_catalogs=legacy_paths,
        )
        body_leads = [{**row, "lead_source": "body"} for row in body_read["leads"]]
        body_search = search_stores(
            verified_stores, canonical_ticker, query, window_start, window_end, cutoff,
            mode=mode, correction_table_id=correction_table_id, store_ids=body_ids,
            correction_tables=body_tables, correction_store_ids=body_bindings,
            correction_artifacts=body_artifacts,
            allowed_legacy_cohorts=legacy_ids,
            legacy_catalogs=legacy_paths,
        )
        evidence = body_search["results"]
        body_read_audit = body_read["audit"]
        search_audit = body_search["audit"]

    metadata_leads: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    metadata_audits = []
    by_table_id = {table.table_id: table for table in tables}
    for store_id, adapter in metadata_pairs:
        selected_table = None
        selected_artifact = None
        selected_scope = None
        if store_id in bindings:
            selected_table = by_table_id.get(correction_table_id)
            if selected_table is None:
                raise ValueError("metadata correction binding requires the selected correction table")
            selected_artifact = artifacts.get(selected_table.table_id)
            if selected_artifact is None:
                raise ValueError("metadata correction table has no pinned artifact")
            selected_scope = bindings[store_id]
        result = read_store_facts(
            verified_stores, store_id, adapter, canonical_ticker,
            window_start, window_end, cutoff, mode=mode,
            registry_path=registry_path,
            session_dates=normalized_market_sessions.get(store_id, ()),
            market_source_id=declared_market_source_ids.get(store_id),
            correction_table_id=(selected_table.table_id if selected_table else None),
            correction_tables=((selected_table,) if selected_table else ()),
            correction_store_ids=({store_id: selected_scope} if selected_table else {}),
            correction_artifacts=({selected_table.table_id: selected_artifact}
                                  if selected_table else {}),
        )
        metadata_leads.extend({**row, "lead_source": "metadata"}
                              for row in result["metadata_leads"])
        facts.extend(result["market_facts"])
        metadata_audits.append({
            "store_id": store_id,
            "adapter": adapter,
            "audit": result["audit"],
        })

    readiness = evaluate_snapshots(requirement_rows, plan, verified_stores, cutoff=cutoff)
    # Keep separate source identities; do not deduplicate article IDs across
    # temporal body leads and metadata-store leads.
    leads = body_leads + metadata_leads
    leads.sort(key=lambda row: (
        row.get("published_at") or row.get("eligible_at") or "",
        row["lead_source"], row.get("store_id", ""),
        row.get("item_id", row.get("version_id", "")),
    ))
    facts.sort(key=lambda row: (row.get("date") or row.get("timestamp") or "", row.get("source_id", "")))
    _verify_all_inputs(verified_stores)
    return {
        "evidence": evidence,
        "leads": leads,
        "facts": facts,
        "readiness": readiness,
        "audit": {
            "manifest_sha256": verified_stores.manifest_sha256,
            "mode": mode,
            "window_start": audit_window_start,
            "window_end": audit_window_end,
            "cutoff": audit_cutoff,
            "data_snapshot_ids": {
                store_id: pin.sha256
                for store_id, pin in sorted(verified_stores.stores.items())
            },
            "body_store_ids": list(body_ids),
            "metadata_stores": [
                {"store_id": store_id, "adapter": adapter}
                for store_id, adapter in metadata_pairs
            ],
            "market_sessions": {
                store_id: list(sessions)
                for store_id, sessions in sorted(normalized_market_sessions.items())
            },
            "market_source_ids": dict(sorted(declared_market_source_ids.items())),
            "body_as_of": body_read_audit,
            "body_search": search_audit,
            "metadata_reads": metadata_audits,
            "readiness": readiness["audit"],
            "coverage_identity_format": readiness["audit"]["coverage_identity_format"],
            "coverage_identity_version": readiness["audit"]["coverage_identity_version"],
            "coverage_requirements_sha256": readiness["audit"]["coverage_requirements_sha256"],
            "coverage_plan_sha256": readiness["audit"]["coverage_plan_sha256"],
            "correction_table_ids": sorted(consumed_correction_ids),
            "correction_store_ids": dict(sorted(bindings.items())),
            "allowed_legacy_cohorts": list(allowed_legacy_cohorts),
            "legacy_catalog_ids": sorted(legacy_ids),
            "evidence_count": len(evidence),
            "lead_count": len(leads),
            "fact_count": len(facts),
            "read_only": True,
        },
    }


__all__ = ["query_stores"]
