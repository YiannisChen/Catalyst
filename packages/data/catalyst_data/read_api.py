"""Read-only entry points for consumers of the data layer (contract v1.0): ``as_of``, ``gap_table`` and the prepared
range. Nothing here writes: ``as_of`` reads manifest-pinned snapshots, ``gap_table`` opens the store ``mode=ro``.

``as_of`` is the narrow door: the mode is required (there is no default) and every time must carry a timezone."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from catalyst_data.dedup import ingestion, rules_v3
from catalyst_data.dedup import relations as dedup_relations
from catalyst_data.ingestion import gap_readiness, source_categories
from catalyst_data.ingestion.collectors import market
from catalyst_data.ingestion.coverage_readiness import CoveragePlan, CoverageRequirement
from catalyst_data.ingestion.gaps import GapLedger
from catalyst_data.ingestion.ops import ready_snapshot as ready_snapshot_store
from catalyst_data.ingestion.time import timefields
from catalyst_data.read import readiness
from catalyst_data.storage.as_of_stores import as_of_stores
from catalyst_data.storage.connect import connect_readonly
from catalyst_data.storage.snapshots import (
    VerifiedStores,
    readonly_snapshot,
    validate_verified_stores,
)
from catalyst_data.storage.time_corrections import load_corrections

MODES = ("observed", "published")

# What a consumer may rely on. Anything else in a store row is internal and may change with the schema.
EVIDENCE_FIELDS = (
    "version_id", "ticker", "kind", "title", "text", "url", "publisher", "source_class", "source_id", "provenance",
    "published_at", "effective_published_at", "observed_at", "first_seen_at", "temporal_status",
    "store_id", "snapshot_id",
)

prepared_pairs = readiness.prepared_pairs
prepared_sessions = readiness.prepared_sessions
prepared_tickers = readiness.prepared_tickers
is_prepared = readiness.is_prepared

_IDENTITY_FIELDS = (
    "record_key", "version_id", "version", "content_hash", "first_seen_at", "observed_at", "eligible_at",
    "published_at_precision", "time_confirmation", "origin", "usage_ceiling",
)
_IDENTITY_TIME_FIELDS = frozenset({"first_seen_at", "observed_at", "eligible_at"})
_D1_EVIDENCE_FIELDS = (
    "dedup_record_key", "origin", "origin_status", "origin_basis", "relation_ids",
    "origin_registry_sha256",
)


def _canonical_value(value: Any) -> Any:
    """Convert datetimes to contract UTC seconds before canonical JSON encoding."""
    if isinstance(value, datetime):
        # Contract §3.0 uses second precision; stored microseconds are truncated here.
        return timefields.stamp(value)
    if isinstance(value, dict):
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        _canonical_value(value), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def _identity_time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        # Stored timestamps are parsed only through the central timefields module.
        parsed = timefields.stored(value)
    # Contract §3.0 is second precision, so sub-second values are truncated.
    return timefields.stamp(parsed)


def _ordered_identities(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: (
        _identity_time(row.get("eligible_at")) or "", row.get("store_id") or "", row.get("source_id") or "",
        row.get("document_id") or "", row.get("version_id") or "",
    ))
    identities = []
    for row in ordered:
        identity = {field: row.get(field) for field in _IDENTITY_FIELDS}
        if "dedup_record_key" in row:
            for field in ("origin_status", "origin_basis", "relation_ids", "origin_registry_sha256"):
                identity[field] = row.get(field)
        for field in _IDENTITY_TIME_FIELDS:
            identity[field] = _identity_time(identity[field])
        # Contract §1: content_hash is the SHA-256 of the original fetched bytes. No store keeps those
        # bytes or their digest; body_sha256 hashes the stored, cleaned text, so it is not that value.
        identity["content_hash"] = None
        identities.append(identity)
    return identities


def _migration_manifest_sha256(audit: dict[str, Any]) -> str | None:
    catalogs = audit.get("legacy_catalogs") or {}
    if not catalogs:
        return None
    # The audit records one catalog manifest digest per cohort. The v1.1 envelope
    # field is singular, so only a single cohort has an unambiguous value here.
    if len(catalogs) == 1:
        return next(iter(catalogs.values())).get("catalog_sha256")
    return None


def _envelope(result: dict[str, Any], t: datetime | None, window_start: datetime,
              window_end: datetime, mode: str, contract_version: str,
              dedup_identity: dict[str, Any] | None = None) -> dict[str, Any]:
    audit = result["audit"]
    data_snapshot_ids = audit.get("data_snapshot_ids") or {}
    input_snapshot_ids = audit.get("input_snapshot_ids") or {}
    correction_table_ids = audit.get("correction_table_ids") or []
    correction_store_ids = audit.get("correction_store_ids") or {}
    envelope = {
        "contract_version": contract_version,
        "mode": mode,
        "observed_as_of": None if t is None else timefields.stamp(t),
        "window_start": timefields.stamp(window_start),
        "window_end": timefields.stamp(window_end),
        "snapshot_identity": {
            "store_ids": sorted(data_snapshot_ids),
            "data_snapshot_id": audit.get("data_snapshot_id"),
            "data_snapshot_ids": data_snapshot_ids,
            "input_snapshot_ids": input_snapshot_ids,
            "manifest_sha256": audit.get("manifest_sha256"),
        },
        "correction_table": {
            "enabled": bool(correction_table_ids or correction_store_ids),
            "correction_table_id": audit.get("correction_table_id"),
            "table_ids": correction_table_ids,
            "store_ids": correction_store_ids,
            "rule_version": None,
        },
        "dedup": dedup_identity or {
            "rule_version": None,
            "relation_ids": [],
            "relation_set_sha256": None,
            "status": "not_applied",
        },
        "migration_manifest_sha256": _migration_manifest_sha256(audit),
        # Every cohort's catalog digest, so a multi-cohort read never hashes like a read with none.
        "legacy_catalog_sha256": {
            cohort: (catalog or {}).get("catalog_sha256")
            for cohort, catalog in sorted((audit.get("legacy_catalogs") or {}).items())
        },
        # Contract §3.0 identities the read path does not carry yet: stated, not guessed.
        "issuer_mapping": {"version": None, "sha256": None, "status": "not_recorded"},
        "source_role_registry": {"version": None, "sha256": None, "status": "not_recorded"},
        "evidence": _ordered_identities(result["evidence"]),
        "leads": _ordered_identities(result["leads"]),
    }
    envelope["evidence_set_hash"] = hashlib.sha256(
        _canonical_json({"evidence": envelope["evidence"], "leads": envelope["leads"]})
    ).hexdigest()
    identity_without_hashes = {
        key: value for key, value in envelope.items()
        if key not in {"evidence_set_hash", "data_read_identity_hash"}
    }
    envelope["data_read_identity_hash"] = hashlib.sha256(_canonical_json(identity_without_hashes)).hexdigest()
    return envelope


def evidence_dto(row: dict) -> dict[str, Any]:
    """The stable view of one evidence or lead row. Missing fields are None, never guessed."""
    result = {field: row.get(field) for field in EVIDENCE_FIELDS}
    if "dedup_record_key" in row:
        result.update({field: row.get(field) for field in _D1_EVIDENCE_FIELDS})
    return result


def _attach_d1_origins(
    verified: VerifiedStores,
    result: dict[str, Any],
    *,
    dedup_store_id: str,
    mode: str,
    at: datetime,
    snapshot_relation_ids: Sequence[str] | None,
    registry_as_of: datetime | None,
) -> dict[str, Any]:
    """Attach D1 v3 results by the exact source-version identity used at ingestion."""
    d1_identity = rules_v3.identity()
    actual_relation_ids: set[str] = set()
    relation_snapshot = None if snapshot_relation_ids is None else set(snapshot_relation_ids)
    with readonly_snapshot(verified, verified.stores[dedup_store_id]) as conn:
        for rows in (result["evidence"], result["leads"]):
            for row in rows:
                try:
                    record_key = ingestion.record_key_for(
                        row["store_id"], row["source_id"], row["document_id"], row["version_id"],
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("read result lacks the exact D1 source-version identity") from exc
                try:
                    origin = rules_v3.assign_origin(
                        conn, record_key, at=timefields.stamp(at), mode=mode,
                        snapshot_relation_ids=relation_snapshot,
                        registry_as_of=(None if registry_as_of is None else timefields.stamp(registry_as_of)),
                    )
                except ValueError as exc:
                    if "record_key is not present in the candidate record index" in str(exc):
                        raise ValueError("D1 v3 record is not indexed in the selected dedup store") from exc
                    raise
                row["record_key"] = record_key
                row["dedup_record_key"] = record_key
                row["origin"] = origin["origin"]
                row["origin_status"] = origin["origin_status"]
                row["origin_basis"] = origin["origin_basis"]
                row["relation_ids"] = origin["relation_ids"]
                row["origin_registry_sha256"] = origin["registry_sha256"]
                actual_relation_ids.update(origin["relation_ids"])

    identity = {
        "status": "applied",
        "store_id": dedup_store_id,
        "relation_ids": sorted(actual_relation_ids),
        "relation_set_sha256": dedup_relations.relation_set_sha256(actual_relation_ids),
        **d1_identity,
    }
    result["audit"]["dedup"] = identity
    return identity


def as_of(verified: VerifiedStores, ticker: str, window_start: datetime, window_end: datetime, t: datetime | None, *,
          mode: str, correction_table_id: str = "none", dedup_store_id: str | None = None,
          dedup_snapshot_relation_ids: Sequence[str] | None = None,
          dedup_registry_as_of: datetime | None = None, **options) -> dict[str, Any]:
    """What was knowable about ``ticker`` at ``t``, for items published in ``[window_start, window_end)``.

    ``observed``: the row must have been first seen by us at or before ``t``. ``published``: the source's own
    publication time must be at or before ``t`` and the item unchanged afterwards. Returns ``evidence`` and ``leads``
    as DTOs plus the full ``audit`` of the read; extra ``options`` go to ``as_of_stores`` unchanged."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}; there is no default")
    named = [("window_start", window_start), ("window_end", window_end)]
    if t is not None or mode == "observed":
        named.append(("t", t))
    for name, value in named:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError(f"{name} must be a timezone-aware datetime")
    dedup_identity = None
    if dedup_store_id is None:
        if dedup_snapshot_relation_ids is not None or dedup_registry_as_of is not None:
            raise ValueError("D1 snapshot and registry inputs require dedup_store_id")
    else:
        if (not isinstance(dedup_store_id, str) or not dedup_store_id.strip()
                or dedup_store_id != dedup_store_id.strip() or dedup_store_id not in verified.stores):
            raise ValueError("dedup_store_id must select a verified D1 store")
        if t is None:
            raise ValueError("D1 reads require an explicit timezone-aware t")
        if mode == "published":
            if (not isinstance(dedup_snapshot_relation_ids, (list, tuple, set, frozenset))
                    or any(not isinstance(item, str) or not item for item in dedup_snapshot_relation_ids)):
                raise ValueError("published D1 reads require dedup_snapshot_relation_ids")
            if not isinstance(dedup_registry_as_of, datetime) or dedup_registry_as_of.tzinfo is None:
                raise ValueError("published D1 reads require a timezone-aware dedup_registry_as_of")
        elif dedup_snapshot_relation_ids is not None or dedup_registry_as_of is not None:
            raise ValueError("observed D1 reads use t and do not accept a published relation snapshot")
        selected_store_ids = options.get("store_ids")
        if selected_store_ids is None:
            selected_store_ids = tuple(sorted(set(verified.stores) - {dedup_store_id}))
        if (not isinstance(selected_store_ids, (list, tuple)) or not selected_store_ids
                or any(not isinstance(item, str) for item in selected_store_ids)
                or dedup_store_id in selected_store_ids):
            raise ValueError("store_ids must select data stores and exclude dedup_store_id")
        options["store_ids"] = tuple(selected_store_ids)
    result = as_of_stores(verified, ticker, window_start, window_end, t, mode=mode,
                          correction_table_id=correction_table_id, **options)
    if dedup_store_id is not None:
        dedup_identity = _attach_d1_origins(
            verified, result, dedup_store_id=dedup_store_id, mode=mode, at=t,
            snapshot_relation_ids=dedup_snapshot_relation_ids,
            registry_as_of=dedup_registry_as_of,
        )
    response = {"mode": mode, "t": None if t is None else t.isoformat(),
                "evidence": [evidence_dto(r) for r in result["evidence"]],
                "leads": [evidence_dto(r) for r in result["leads"]], "audit": result["audit"]}
    # The old audit remains unchanged (including its v1.0 default); the additive
    # envelope advertises the new consumer contract version unless explicitly set.
    response["envelope"] = _envelope(
        result, t, window_start, window_end, mode, options.get("contract_version", "data-core-v1.1"),
        dedup_identity,
    )
    return response


class ReadySnapshotError(ValueError):
    """A ready snapshot is missing, malformed, or internally inconsistent."""


def ready_snapshot(path: str | Path | None = None) -> dict[str, Any]:
    """Read and validate the daily prepared-scope snapshot; errors fail closed."""
    source = Path(path) if path is not None else ready_snapshot_store.default_path()
    try:
        with source.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReadySnapshotError(f"cannot read ready snapshot: {exc}") from exc
    required = {"schema_version", "generated_at", "pairs_sha256", "daily_last_completed_at",
                "last_completed_session_date", "pairs"}
    if not isinstance(value, dict) or set(value) != required or value.get("schema_version") != 2:
        raise ReadySnapshotError("invalid ready snapshot schema")
    if not isinstance(value["generated_at"], str) or not value["generated_at"].endswith("Z"):
        raise ReadySnapshotError("invalid ready snapshot generated_at")
    try:
        datetime.fromisoformat(value["generated_at"].removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ReadySnapshotError("invalid ready snapshot generated_at") from exc
    for field in ("daily_last_completed_at", "last_completed_session_date"):
        if value[field] is not None and not isinstance(value[field], str):
            raise ReadySnapshotError(f"invalid ready snapshot {field}")
    pairs = value["pairs"]
    if not isinstance(pairs, list) or any(not isinstance(pair, dict) or set(pair) != {"ticker", "session_date"}
            or not all(isinstance(pair[key], str) and pair[key] for key in ("ticker", "session_date"))
            for pair in pairs):
        raise ReadySnapshotError("invalid ready snapshot pairs")
    canonical = json.dumps(pairs, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                           allow_nan=False).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    if value["pairs_sha256"] != digest:
        raise ReadySnapshotError("ready snapshot pairs_sha256 mismatch")
    return value


def ready_range(verified: VerifiedStores, *, jobs_store_id: str,
                requirements_for: Callable[[readiness.PreparedPair], Sequence[CoverageRequirement]],
                plan: CoveragePlan, contract_version: str,
                required_categories: Sequence[str] = source_categories.CATEGORIES,
                cutoff: datetime | None = None, snapshot_id: str | None = None) -> dict[str, Any]:
    """Return prepared pairs whose caller-declared required categories are complete."""
    if jobs_store_id not in verified.stores:
        raise ValueError("jobs_store_id is not present in verified stores")
    categories = tuple(required_categories)
    if not categories or len(set(categories)) != len(categories) or not set(categories) <= set(source_categories.CATEGORIES):
        raise ValueError("required_categories must be a non-empty unique subset of source categories")
    if cutoff is not None:
        if not isinstance(cutoff, datetime) or cutoff.utcoffset() is None:
            raise ValueError("cutoff must be a timezone-aware datetime")
        cutoff = cutoff.astimezone(timezone.utc)
    # Coverage facts are compared at microsecond precision, so the identity keeps the same precision.
    cutoff_identity = None if cutoff is None else cutoff.isoformat(timespec="microseconds").replace("+00:00", "Z")

    with readonly_snapshot(verified, verified.stores[jobs_store_id]) as conn:
        prepared = readiness.prepared_pairs_from_connection(conn)

    all_requirements: list[CoverageRequirement] = []
    for pair in prepared:
        declared = list(requirements_for(pair))
        for requirement in declared:
            if requirement.session_date != pair.session_date or requirement.ticker != pair.ticker:
                raise ValueError("requirement ticker and session_date must match its prepared pair")
        all_requirements.extend(declared)

    # Hashed even with no requirement, so the plan is always part of the identity.
    requirements_sha256, plan_sha256 = gap_readiness.coverage_identity_sha256(all_requirements, plan)
    grouped = {}
    # Pairs with no declared requirement are all NOT_DECLARED; there is nothing to evaluate.
    if all_requirements:
        evaluation = gap_readiness.evaluate_snapshots(all_requirements, plan, verified, cutoff=cutoff)
        grouped = {
            (row["session_date"], row["ticker"], row["category"]): row
            for row in evaluation["results"]
        }

    complete_pairs: list[dict[str, str]] = []
    incomplete_pairs: list[dict[str, Any]] = []
    for pair in prepared:
        pair_key = (pair.session_date, pair.ticker)
        category_results = {}
        for category in categories:
            row = grouped.get((*pair_key, category))
            if row is None:
                category_results[category] = {
                    "status": "NOT_DECLARED", "reason": None, "complete": False,
                    "missing_requirement_ids": [],
                }
            else:
                category_results[category] = {
                    "status": row["status"], "reason": row["reason"], "complete": row["complete"],
                    "missing_requirement_ids": row["missing_requirement_ids"],
                }
        item = {"ticker": pair.ticker, "session_date": pair.session_date}
        if all(result["complete"] for result in category_results.values()):
            complete_pairs.append(item)
        else:
            incomplete_pairs.append(item | {"categories": category_results})

    data_snapshot_ids = {store_id: pin.sha256 for store_id, pin in sorted(verified.stores.items())}
    identity = {
        "manifest_sha256": verified.manifest_sha256,
        "data_snapshot_ids": data_snapshot_ids,
        "jobs_store_id": jobs_store_id,
        "coverage_requirements_sha256": requirements_sha256,
        "coverage_plan_sha256": plan_sha256,
        "required_categories": sorted(categories),
        "cutoff": cutoff_identity,
        "contract_version": contract_version,
    }
    derived_snapshot_id = "rr1:" + hashlib.sha256(_canonical_json(identity)).hexdigest()
    if snapshot_id is not None and snapshot_id != derived_snapshot_id:
        raise ValueError("snapshot_id does not match the data")
    validate_verified_stores(verified)
    return {
        "pairs": complete_pairs,
        "incomplete_pairs": incomplete_pairs,
        "snapshot_id": derived_snapshot_id,
        "contract_version": contract_version,
        "integrity_basis": {
            "verify_stage": {"qualifying_statuses": ["OK", "EMPTY"], "per_session_any_match": True},
            "movers_stage": {"qualifying_status": "OK", "latest_by": "finished_at", "requires_result_json": True},
            "six_category_check": {
                # With no declared requirement the coverage stores were never consulted.
                "status": "checked" if all_requirements else "not_evaluated_no_requirements",
                "required_categories": list(categories), "cutoff": cutoff_identity,
                "rule": "every required category complete per gap_readiness.evaluate_snapshots",
                "coverage_requirements_sha256": requirements_sha256,
                "coverage_plan_sha256": plan_sha256,
                "data_snapshot_ids": data_snapshot_ids,
                "manifest_sha256": verified.manifest_sha256,
            },
        },
    }


def gap_table(db_path: str | Path, *, session_date: str | None = None, ticker: str | None = None,
              category: str | None = None) -> list[dict]:
    """Category-level coverage rows (no provider ids) from a store's gap ledger, opened read-only. A missing store
    or ledger raises; it is never reported as 'no gaps'."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = connect_readonly(path, timeout_ms=10_000, immutable=False)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='category_gaps'").fetchone() is None:
            raise LookupError(f"{path.name} has no gap ledger (category_gaps)")
        return GapLedger(conn).public_rows(category=category, session_date=session_date, ticker=ticker)
    finally:
        conn.close()


class SecFilingDataError(RuntimeError):
    """The local SEC filing projection is missing or cannot support a requested read."""


class SecCoverageError(SecFilingDataError):
    """The local SEC store does not prove complete coverage for a ticker range."""

    def __init__(self, message: str, *, uncovered: Sequence[Mapping[str, Any]]):
        super().__init__(message)
        self.uncovered = list(uncovered)


def _utc_z(value: str) -> str:
    parsed = timefields.stored(value)
    return timefields.stamp(parsed)


SEC_BODY_STORE_ID = "server:news_bodies_v2.sqlite3"


def _sec_acceptance_correction_table(path: str | Path | None = None):
    corrections_path = Path(path) if path is not None else (
        market.REPO / "data/corrections/sec_acceptance_v1.csv"
    )
    if not corrections_path.is_file():
        raise FileNotFoundError(corrections_path)
    correction_bytes = corrections_path.read_bytes()
    return load_corrections(
        correction_bytes, kind="sec", filename=corrections_path.name,
        expected_sha256=hashlib.sha256(correction_bytes).hexdigest(),
    )


def _sec_corrected_acceptance_at(
    eligible_at: str | None,
    published_at_source: str | None,
    accession: str,
    correction_table,
    *,
    store_id: str = SEC_BODY_STORE_ID,
) -> tuple[str | None, str, str]:
    """Apply the SEC acceptance correction contract shared by reads and corpus publication."""
    if not eligible_at:
        raise SecFilingDataError(f"SEC filing {accession} has missing acceptance time")
    if published_at_source != "sec_index":
        raise SecFilingDataError(
            f"SEC filing {accession} lacks verified EDGAR index-time provenance"
        )
    acceptance_at = _utc_z(eligible_at)
    decision = correction_table.apply(acceptance_at, store=store_id, accession=accession)
    if decision.status in {"corrected", "already_corrected"}:
        return _utc_z(decision.effective_at or acceptance_at), "applied", \
            f"{decision.table_id}#{accession}"
    if decision.status == "unmatched":
        return acceptance_at, "none", "none"
    if decision.status == "not_applicable":
        return None, "not_applicable_store", f"{decision.table_id}#{accession}"
    raise SecFilingDataError(
        f"SEC filing {accession} has unresolved correction status {decision.status}"
    )


def _sec_cik(urls: Sequence[str]) -> str:
    ciks = set()
    for url in urls:
        match = re.search(r"/Archives/edgar/data/(\d+)/", url)
        if match:
            ciks.add(match.group(1).zfill(10))
    if len(ciks) != 1:
        raise SecFilingDataError("SEC filing rows do not identify one unambiguous CIK")
    return next(iter(ciks))


def _sec_coverage_for_tickers(
    conn: sqlite3.Connection,
    tickers: Sequence[str],
    start_date: date,
    end_date: date,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    from catalyst_data.ingestion import gaps

    requested_from = timefields.stored(f"{start_date.isoformat()}T00:00:00Z")
    requested_until = timefields.stored(f"{(end_date + timedelta(days=1)).isoformat()}T00:00:00Z")

    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='category_gaps'"
    ).fetchone()
    if present is None:
        raise SecFilingDataError("SEC coverage ledger is missing table category_gaps")
    required_columns = {
        "session_date", "ticker", "source_id", "scope_kind", "scope_id", "status",
        "coverage_from", "coverage_until", "updated_at", "id", "logical_task_id",
        "mapping_version",
    }
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(category_gaps)")}
        missing_columns = required_columns - columns
        if missing_columns:
            raise SecFilingDataError(
                "SEC coverage ledger has an invalid category_gaps schema: missing "
                + ", ".join(sorted(missing_columns))
            )
        placeholders = ",".join("?" for _ in tickers)
        selected = conn.execute(
            "SELECT * FROM category_gaps WHERE ticker IN (" + placeholders + ") "
            "AND source_id='daily:sec' AND scope_kind='target' "
            "ORDER BY ticker,session_date,updated_at,id",
            tuple(tickers),
        ).fetchall()
        records = [gaps._row_to_record(row) for row in selected]
    except SecFilingDataError:
        raise
    except (sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
        raise SecFilingDataError(f"could not read SEC category_gaps coverage: {exc}") from exc

    latest: dict[tuple[str, str], Any] = {}
    for row in records:
        key = (row.ticker.upper(), row.session_date)
        try:
            rank = (gaps._time_value(row.updated_at), row.id)
            prior = latest.get(key)
            prior_rank = (gaps._time_value(prior.updated_at), prior.id) if prior else None
        except (TypeError, ValueError, OverflowError) as exc:
            raise SecFilingDataError(
                f"SEC category_gaps has an invalid updated_at for {row.ticker} {row.session_date}"
            ) from exc
        if prior_rank is None or rank > prior_rank:
            latest[key] = row

    by_ticker: dict[str, list[dict[str, Any]]] = {ticker: [] for ticker in tickers}
    intervals: dict[str, list[tuple[datetime, datetime]]] = {ticker: [] for ticker in tickers}
    for ticker in tickers:
        ticker_sessions = sorted(
            session_date for record_ticker, session_date in latest if record_ticker == ticker
        )
        for session_date in ticker_sessions:
            row = latest[(ticker, session_date)]
            if (row is None or row.status not in {"OK", "EMPTY"}
                    or not row.coverage_from or not row.coverage_until):
                continue
            try:
                actual_from = gaps._time_value(row.coverage_from)
                actual_until = gaps._time_value(row.coverage_until)
            except (TypeError, ValueError, OverflowError) as exc:
                raise SecFilingDataError(
                    f"SEC category_gaps has invalid coverage bounds for {ticker} {session_date}"
                ) from exc
            if actual_from >= actual_until:
                raise SecFilingDataError(
                    f"SEC category_gaps has an empty coverage window for {ticker} {session_date}"
                )
            if actual_until <= requested_from or actual_from >= requested_until:
                continue
            intervals[ticker].append((actual_from, actual_until))
            by_ticker[ticker].append({
                "from": timefields.stamp(actual_from),
                "until": timefields.stamp(actual_until),
                "source": "category_gaps",
                # category_gaps has no separate source-policy field.
                "policy": None,
                "session_date": row.session_date,
                "status": row.status,
                "updated_at": row.updated_at,
            })

    uncovered = []
    for ticker in tickers:
        missing_ranges = _uncovered_sec_ranges(
            intervals[ticker], requested_from, requested_until,
        )
        if not missing_ranges:
            continue
        uncovered.append({
            "ticker": ticker,
            "missing": [{"from": timefields.stamp(left), "until": timefields.stamp(right)}
                        for left, right in missing_ranges],
        })
    if uncovered:
        details = "; ".join(
            f"{item['ticker']} missing " + ", ".join(
                f"{gap['from']} through {gap['until']}"
                for gap in item["missing"]
            )
            for item in uncovered
        )
        raise SecCoverageError(
            f"SEC filing coverage is incomplete: {details}", uncovered=uncovered,
        )
    return by_ticker, uncovered


def _uncovered_sec_ranges(
    intervals: Sequence[tuple[datetime, datetime]],
    requested_from: datetime,
    requested_until: datetime,
) -> list[tuple[datetime, datetime]]:
    """Return holes in a half-open UTC request interval after merging touching windows."""
    missing: list[tuple[datetime, datetime]] = []
    cursor = requested_from
    for left, right in sorted(intervals):
        left = max(left, requested_from)
        right = min(right, requested_until)
        if right <= cursor:
            continue
        if left > cursor:
            missing.append((cursor, left))
        cursor = max(cursor, right)
        if cursor >= requested_until:
            break
    if cursor < requested_until:
        missing.append((cursor, requested_until))
    return missing


def sec_8k_acceptance_result(
    db_path: str | Path,
    *,
    tickers: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    accessions: Sequence[str] | None = None,
    store_id: str = "server:news_bodies_v2.sqlite3",
    correction_table_path: str | Path | None = None,
    immutable: bool = False,
) -> dict[str, Any]:
    """Read locally stored SEC 8-K records and their scoped acceptance-time corrections.

    Supply either ``accessions`` or ``tickers`` with inclusive UTC calendar-date bounds.
    For ticker/date requests, the union of intersecting latest ``category_gaps`` windows must
    cover the full half-open UTC interval from start midnight through midnight after end.
    Touching windows are continuous. A covered range with no filings returns empty ``rows``;
    accession requests retain their strict missing-row behavior. Filed date is not persisted
    and is returned as ``None`` with a ``missing_fields`` marker.
    """
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    if not isinstance(immutable, bool):
        raise ValueError("immutable must be a boolean")
    if accessions:
        if tickers is not None or start is not None or end is not None:
            raise ValueError("provide accessions or ticker/date filters, not both")
        if not isinstance(accessions, Sequence) or isinstance(accessions, (str, bytes)):
            raise ValueError("accessions must be a sequence of canonical strings")
        requested_accessions = tuple(dict.fromkeys(accessions))
        if any(not isinstance(item, str) or not item.strip() or item != item.strip()
               for item in requested_accessions):
            raise ValueError("accessions must be nonempty canonical strings")
    else:
        if (not isinstance(tickers, Sequence) or isinstance(tickers, (str, bytes)) or not tickers
                or not start or not end):
            raise ValueError("provide accessions or one or more tickers with start and end dates")
        requested_accessions = ()
        try:
            start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
        except (TypeError, ValueError) as exc:
            raise ValueError("start and end must be ISO calendar dates") from exc
        if start_date > end_date:
            raise ValueError("start must not be after end")
        if any(not isinstance(ticker, str) or not ticker.strip() for ticker in tickers):
            raise ValueError("tickers must be nonempty strings")
        normalized_tickers = tuple(dict.fromkeys(ticker.strip().upper() for ticker in tickers))

    correction_table = _sec_acceptance_correction_table(correction_table_path)

    conn = connect_readonly(path, immutable=True if immutable else False, timeout_ms=10_000)
    conn.row_factory = sqlite3.Row
    try:
        required_tables = {"body_versions", "temporal_evidence_map", "temporal_evidence_versions"}
        present_tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        missing_tables = required_tables - present_tables
        if missing_tables:
            raise SecFilingDataError(
                "SEC filing projection is missing tables: " + ", ".join(sorted(missing_tables))
            )
        clauses = ["v.source_id='daily:sec'", "m.kind='filing'", "m.sec_accession IS NOT NULL"]
        params: list[Any] = []
        if requested_accessions:
            clauses.append("m.sec_accession IN (" + ",".join("?" for _ in requested_accessions) + ")")
            params.extend(requested_accessions)
        else:
            clauses.append("UPPER(m.ticker) IN (" + ",".join("?" for _ in normalized_tickers) + ")")
            params.extend(normalized_tickers)
            clauses.append(
                "(m.eligible_at IS NULL OR substr(m.eligible_at, 1, 10) BETWEEN ? AND ?)"
            )
            params.extend(((start_date - timedelta(days=1)).isoformat(),
                           (end_date + timedelta(days=1)).isoformat()))
        rows = conn.execute(
            "SELECT m.ticker,m.sec_accession,m.eligible_at,m.url,v.title,x.published_at_source "
            "FROM temporal_evidence_map m "
            "JOIN body_versions v ON v.version_id=m.version_id "
            "LEFT JOIN temporal_evidence_versions x ON x.version_id=m.version_id "
            "WHERE " + " AND ".join(clauses) +
            " ORDER BY m.sec_accession,m.ticker,m.url",
            params,
        ).fetchall()
    except sqlite3.Error as exc:
        raise SecFilingDataError(f"could not read SEC filing projection: {exc}") from exc
    finally:
        conn.close()

    coverage_by_ticker: dict[str, list[dict[str, Any]]] = {}
    uncovered: list[dict[str, Any]] = []
    if not requested_accessions:
        coverage_conn = connect_readonly(path, immutable=True if immutable else False, timeout_ms=10_000)
        try:
            coverage_conn.row_factory = sqlite3.Row
            coverage_by_ticker, uncovered = _sec_coverage_for_tickers(
                coverage_conn, normalized_tickers, start_date, end_date,
            )
        except sqlite3.Error as exc:
            raise SecFilingDataError(f"could not read SEC coverage facts: {exc}") from exc
        finally:
            coverage_conn.close()

    if not rows:
        if requested_accessions:
            raise SecFilingDataError("no SEC filing rows matched the requested local data")
        return {
            "rows": [], "coverage": coverage_by_ticker, "uncovered": uncovered,
            "unverified_time": [],
        }
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault((row["sec_accession"], row["ticker"].upper()), []).append(row)
    missing_accessions = sorted(set(requested_accessions) - {accession for accession, _ in grouped})
    if missing_accessions:
        raise SecFilingDataError(
            "requested accessions are absent from the local SEC filing projection: "
            + ", ".join(missing_accessions)
        )

    result = []
    unverified_time = []
    request_from = timefields.stored(f"{start_date.isoformat()}T00:00:00Z") if not requested_accessions else None
    request_until = timefields.stored(
        f"{(end_date + timedelta(days=1)).isoformat()}T00:00:00Z"
    ) if not requested_accessions else None
    for (accession, ticker), filing_rows in sorted(grouped.items()):
        raw_times = {row["eligible_at"] for row in filing_rows if row["eligible_at"]}
        if any(not row["eligible_at"] for row in filing_rows):
            raise SecFilingDataError(f"SEC filing {accession} has missing acceptance time")
        if len(raw_times) != 1:
            raise SecFilingDataError(f"SEC filing {accession} has conflicting acceptance times")
        published_sources = {row["published_at_source"] for row in filing_rows}
        if len(published_sources) != 1:
            raise SecFilingDataError(f"SEC filing {accession} has conflicting acceptance provenance")
        corrected_at, correction_status, correction_ref = _sec_corrected_acceptance_at(
            next(iter(raw_times)), next(iter(published_sources)), accession,
            correction_table, store_id=store_id,
        )
        urls = sorted({row["url"] for row in filing_rows if row["url"]})
        if any(not row["url"] for row in filing_rows):
            raise SecFilingDataError(f"SEC filing {accession} has a missing document URL")
        if correction_status == "applied":
            missing_fields = ["filed_date", "form"]
        elif correction_status == "not_applicable_store":
            missing_fields = ["filed_date", "form", "corrected_at"]
        elif correction_status == "none":
            missing_fields = ["filed_date", "form"]
        if corrected_at is None:
            unverified_time.append({
                "accession": accession,
                "acceptance_at": _utc_z(next(iter(raw_times))),
                "correction_ref": correction_ref,
            })
        elif not requested_accessions:
            corrected_datetime = timefields.stored(corrected_at)
            if not request_from <= corrected_datetime < request_until:
                continue
            filing_windows = [
                (timefields.stored(window["from"]), timefields.stored(window["until"]))
                for window in coverage_by_ticker[ticker]
            ]
            if not any(left <= corrected_datetime < right for left, right in filing_windows):
                raise SecFilingDataError(
                    f"SEC filing {accession} corrected_at is outside the proven category_gaps windows"
                )
        result.append({
            "accession": accession,
            "cik": _sec_cik(urls),
            "ticker": ticker,
            "form": None,
            "filed_date": None,
            "acceptance_at": _utc_z(next(iter(raw_times))),
            "corrected_at": corrected_at,
            "correction_ref": correction_ref,
            "correction_status": correction_status,
            "exhibits": urls,
            "missing_fields": missing_fields,
        })
    if not result:
        if requested_accessions:
            raise SecFilingDataError("no SEC filing rows matched the requested local data")
    return {
        "rows": result, "coverage": coverage_by_ticker, "uncovered": uncovered,
        "unverified_time": unverified_time,
    }


def sec_8k_acceptance_table(
    db_path: str | Path,
    *,
    tickers: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    accessions: Sequence[str] | None = None,
    store_id: str = "server:news_bodies_v2.sqlite3",
    correction_table_path: str | Path | None = None,
    immutable: bool = False,
) -> list[dict[str, Any]]:
    """Return SEC filing rows using the coverage-aware acceptance result API."""
    return sec_8k_acceptance_result(
        db_path, tickers=tickers, start=start, end=end, accessions=accessions,
        store_id=store_id, correction_table_path=correction_table_path, immutable=immutable,
    )["rows"]


def _next_calendar_trading_day(day: date) -> str:
    from catalyst_data import trading_calendar

    candidate = day + timedelta(days=1)
    while True:
        candidate_text = candidate.isoformat()
        # Validate support before is_trading_day can classify an unknown year as closed.
        trading_calendar.calendar_trading_days(candidate_text, candidate_text)
        if trading_calendar.is_trading_day(candidate_text):
            return candidate_text
        candidate += timedelta(days=1)


def _release_session_fields(corrected_at: str) -> dict[str, str]:
    from catalyst_data import trading_calendar

    accepted = timefields.stored(corrected_at)
    local = accepted.astimezone(trading_calendar.ET)
    session_date = local.date().isoformat()
    # Check coverage explicitly; is_trading_day returns False outside its coverage.
    trading_calendar.calendar_trading_days(session_date, session_date)

    if not trading_calendar.is_trading_day(session_date):
        release_session = "non_trading_day"
        attributed_session_date = _next_calendar_trading_day(local.date())
    else:
        market_open = timefields.stored(trading_calendar.session_open_utc(session_date))
        market_close = timefields.stored(trading_calendar.session_close_utc(session_date))
        if accepted < market_open:
            release_session = "pre_market"
            attributed_session_date = session_date
        elif accepted < market_close:
            release_session = "intraday"
            attributed_session_date = session_date
        else:
            release_session = "after_close"
            attributed_session_date = _next_calendar_trading_day(local.date())

    return {
        "release_session": release_session,
        "release_local_time": local.isoformat(timespec="seconds"),
        "attributed_session_date": attributed_session_date,
    }


def _submission_items_for_accession(
    submissions_dir: str | Path, cik: str, accession: str,
) -> bool | None:
    """Read Item 2.02 from cached SEC submissions pages; never refresh the cache."""
    directory = Path(submissions_dir).expanduser()
    if not directory.is_dir():
        return None
    paths = sorted(
        set(directory.glob(f"{cik}.json"))
        | set(directory.glob(f"CIK{cik}.json"))
        | set(directory.glob(f"CIK{cik}-submissions-*.json"))
    )
    matches: set[bool] = set()
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SecFilingDataError(
                f"could not read local SEC submissions cache {path.name}"
            ) from exc
        if not isinstance(payload, dict):
            raise SecFilingDataError(f"local SEC submissions cache {path.name} is not an object")
        filings = payload.get("filings", payload)
        if not isinstance(filings, dict):
            raise SecFilingDataError(f"local SEC submissions cache {path.name} has invalid filings")
        recent = filings.get("recent")
        if isinstance(recent, dict):
            batches = [recent]
        elif isinstance(filings.get("accessionNumber"), list):
            batches = [filings]
        else:
            # The current page may only reference archived pages, scanned above.
            continue

        for batch in batches:
            accession_rows, item_rows = batch.get("accessionNumber"), batch.get("items")
            if (not isinstance(accession_rows, list) or not isinstance(item_rows, list)
                    or len(accession_rows) != len(item_rows)):
                raise SecFilingDataError(
                    f"local SEC submissions cache {path.name} has invalid filing arrays"
                )
            for candidate, raw_items in zip(accession_rows, item_rows):
                if candidate != accession:
                    continue
                if isinstance(raw_items, str):
                    items = {item.strip() for item in raw_items.split(",") if item.strip()}
                elif isinstance(raw_items, list) and all(isinstance(item, str) for item in raw_items):
                    items = {item.strip() for item in raw_items if item.strip()}
                else:
                    raise SecFilingDataError(f"SEC submissions items are invalid for {accession}")
                matches.add("2.02" in items)

    if len(matches) > 1:
        raise SecFilingDataError(f"local SEC submissions pages conflict on items for {accession}")
    return next(iter(matches)) if matches else None


def _main_document_item_202(
    db_path: str | Path, accession: str, *, immutable: bool,
) -> bool | None:
    """Read Item 2.02 only from stored units for the filing's primary 8-K document."""
    conn = connect_readonly(Path(db_path), immutable=True if immutable else False, timeout_ms=10_000)
    conn.row_factory = sqlite3.Row
    try:
        has_units = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sec_exhibit_units'"
        ).fetchone()
        if has_units is None:
            raise SecFilingDataError(
                f"body store is missing sec_exhibit_units table while reading {accession}"
            )
        rows = conn.execute(
            "SELECT DISTINCT u.units_json, v.title "
            "FROM sec_exhibit_units u "
            "JOIN temporal_evidence_map m ON m.url=u.url "
            "JOIN body_versions v ON v.version_id=m.version_id "
            "WHERE u.accession=? AND m.sec_accession=? AND m.kind='filing'",
            (accession, accession),
        ).fetchall()
    except sqlite3.Error as exc:
        raise SecFilingDataError(f"could not read SEC exhibit units for {accession}: {exc}") from exc
    finally:
        conn.close()

    main_documents: list[list[str]] = []
    for row in rows:
        title = str(row["title"] or "")
        if re.search(r"\bForm 8-K\s+8-K(?:/A)?\s*:", title, re.IGNORECASE) is None:
            continue
        try:
            structure = json.loads(row["units_json"])
        except (TypeError, ValueError) as exc:
            raise SecFilingDataError(f"stored SEC exhibit units are invalid for {accession}") from exc
        units = structure.get("units") if isinstance(structure, dict) else None
        if not isinstance(units, list):
            raise SecFilingDataError(f"stored SEC main-document units are invalid for {accession}")
        main_documents.append([
            unit["text"] for unit in units
            if isinstance(unit, dict) and isinstance(unit.get("text"), str)
        ])

    if not main_documents or any(units != main_documents[0] for units in main_documents[1:]):
        return None

    item_heading = re.compile(
        r"^\s*Item\s+(\d{1,2}\.\d{2})\b(?:\s*[:.\-–—]\s*|\s+)(.*)$",
        re.IGNORECASE,
    )
    results_heading = re.compile(
        r"^Results\s+of\s+Operations\s+and\s+Financial\s+Condition\b",
        re.IGNORECASE,
    )
    recognized_item_heading = False
    earnings_heading = False
    uncertain_earnings_heading = False
    for text in main_documents[0]:
        match = item_heading.match(text)
        if match is None:
            continue
        recognized_item_heading = True
        if match.group(1) == "2.02":
            if results_heading.match(match.group(2)):
                earnings_heading = True
            else:
                uncertain_earnings_heading = True

    if earnings_heading:
        return True
    if uncertain_earnings_heading:
        return None
    return False if recognized_item_heading else None


def earnings_release_timing(
    db_path: str | Path,
    *,
    tickers: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    accessions: Sequence[str] | None = None,
    store_id: str = "server:news_bodies_v2.sqlite3",
    correction_table_path: str | Path | None = None,
    immutable: bool = False,
    ticker_mapping_path: str | Path | None = None,
    submissions_dir: str | Path | None = None,
    timing_callback: Any | None = None,
) -> list[dict[str, Any]]:
    """Read corrected 8-K timing and issuer-earnings classification from local stores only.

    Filing filters and correction semantics match :func:`sec_8k_acceptance_table`.
    The injectable mapping and submissions paths make local-cache reads deterministic.
    ``timing_callback`` is accepted so the existing caller keeps working. This
    data-side table does not invoke it; the caller times the read itself.
    """
    del timing_callback
    from catalyst_data.ingestion.collectors import sec, sec_filings

    rows = sec_8k_acceptance_table(
        db_path, tickers=tickers, start=start, end=end, accessions=accessions,
        store_id=store_id, correction_table_path=correction_table_path, immutable=immutable,
    )
    mapping_path = Path(ticker_mapping_path).expanduser() if ticker_mapping_path else sec._tickers_path()
    submissions_path = (
        Path(submissions_dir).expanduser() if submissions_dir else sec_filings.cache_dir()
    )
    mapping: dict[str, Any] | None = None
    if mapping_path.is_file():
        try:
            loaded = json.loads(mapping_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SecFilingDataError(f"could not read SEC ticker mapping {mapping_path.name}") from exc
        if not isinstance(loaded, dict):
            raise SecFilingDataError("SEC ticker mapping must be a JSON object")
        mapping = loaded

    result = []
    for source in rows:
        row = dict(source)
        missing = list(row.get("missing_fields") or ())
        if row["correction_status"] == "not_applicable_store":
            row.update({
                "release_session": None,
                "release_local_time": None,
                "attributed_session_date": None,
            })
            for field in (
                "corrected_at", "release_session", "release_local_time", "attributed_session_date",
            ):
                if field not in missing:
                    missing.append(field)
        else:
            row.update(_release_session_fields(row["corrected_at"]))

        filing_cik = row["cik"]
        issuer_cik = sec.cik_for(row["ticker"], mapping) if mapping is not None else None
        issuer_cik_match = None if issuer_cik is None else filing_cik == issuer_cik
        if issuer_cik_match is None and "issuer_cik_match" not in missing:
            missing.append("issuer_cik_match")

        item_202 = _submission_items_for_accession(
            submissions_path, filing_cik, row["accession"],
        )
        earnings_basis = "submissions_items"
        if item_202 is None:
            item_202 = _main_document_item_202(db_path, row["accession"], immutable=immutable)
            earnings_basis = "exhibit_units_text" if item_202 is not None else "unknown"
        if issuer_cik_match is False:
            is_issuer_earnings = False
        elif issuer_cik_match is True and item_202 is not None:
            is_issuer_earnings = item_202
        else:
            is_issuer_earnings = None
        if is_issuer_earnings is None and "is_issuer_earnings" not in missing:
            missing.append("is_issuer_earnings")

        row.update({
            "issuer_cik_match": issuer_cik_match,
            "is_issuer_earnings": is_issuer_earnings,
            "earnings_basis": earnings_basis,
            "missing_fields": missing,
        })
        result.append(row)
    return result
