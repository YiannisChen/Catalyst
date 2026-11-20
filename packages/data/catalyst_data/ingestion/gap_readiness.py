"""Role-aware coverage projection over manifest-verified SQLite snapshots."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from catalyst_data.ingestion import coverage
from catalyst_data.ingestion.coverage_readiness import (
    CoveragePlan,
    CoverageRequirement,
    _evaluate_coverage,
    evaluate_coverage,
)
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage.snapshots import (
    VerifiedStores,
    readonly_snapshot,
    validate_verified_stores,
)

_TABLE_COLUMNS = {
    "source_coverage_attempts": tuple(coverage._ATTEMPT_COLUMNS),
    "source_coverage_facts": tuple(coverage._FACT_COLUMNS),
    "source_coverage_targets": ("coverage_id", "identity", "acquired"),
}
_COVERAGE_IDENTITY_FORMAT = "canonical-json-sha256"
_COVERAGE_IDENTITY_VERSION = 1


def _schema(conn: sqlite3.Connection, store_id: str) -> tuple[str, ...]:
    names = set(_TABLE_COLUMNS)
    objects = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT name,type FROM sqlite_master WHERE name IN (?,?,?)",
            tuple(_TABLE_COLUMNS),
        )
    }
    if not objects:
        return ()
    if any(kind != "table" for kind in objects.values()):
        raise ValueError(f"store {store_id} has an invalid G1 coverage schema: non-table object")
    if set(objects) != names:
        raise ValueError(f"store {store_id} has a partial G1 coverage schema")
    for name, expected in _TABLE_COLUMNS.items():
        actual = tuple(row[1] for row in conn.execute(f'PRAGMA table_info("{name}")'))
        if actual != expected:
            raise ValueError(f"store {store_id} has an invalid G1 coverage schema")
    return tuple(_TABLE_COLUMNS)


def _history(conn: sqlite3.Connection, store_id: str):
    if not _schema(conn, store_id):
        return None
    attempts = [tuple(row) for row in conn.execute(
        f"SELECT {','.join(coverage._ATTEMPT_COLUMNS)} FROM source_coverage_attempts ORDER BY partition_id,attempt_seq"
    )]
    facts = [tuple(row) for row in conn.execute(
        f"SELECT {','.join(coverage._FACT_COLUMNS)} FROM source_coverage_facts ORDER BY attempt_id,state_seq"
    )]
    targets = [tuple(row) for row in conn.execute(
        "SELECT coverage_id,identity,acquired FROM source_coverage_targets ORDER BY coverage_id,identity"
    )]
    for row in attempts:
        if type(row[3]) is not int or row[3] <= 0:
            raise ValueError(f"store {store_id} has an invalid attempt sequence")
        if any(not isinstance(row[index], str) or not row[index] for index in (0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12)):
            raise ValueError(f"store {store_id} has invalid attempt fields")
    for row in facts:
        if type(row[4]) is not int or row[4] <= 0 or type(row[5]) is not int or row[5] <= 0:
            raise ValueError(f"store {store_id} has an invalid coverage sequence")
        if row[17] is not None and (type(row[17]) is not int or row[17] not in (0, 1)):
            raise ValueError(f"store {store_id} has invalid enumeration_complete data")
        for index in (19, 20):
            if row[index] is not None and (type(row[index]) is not int or row[index] < 0):
                raise ValueError(f"store {store_id} has invalid coverage count data")
        if row[19] is not None and row[20] is not None and row[20] > row[19]:
            raise ValueError(f"store {store_id} has inconsistent coverage counts")
        if row[21] not in {"OK", "EMPTY", "ERROR", "NOT_FETCHED"}:
            raise ValueError(f"store {store_id} has an invalid coverage status")
        allowed_reasons = {"OK": {None}, "EMPTY": {None}, "ERROR": {"ERROR"},
                           "NOT_FETCHED": {"DISABLED", "deferred", "breaker"}}
        if row[22] not in allowed_reasons[row[21]]:
            raise ValueError(f"store {store_id} has an invalid coverage reason")
        try:
            timefields.stored(row[23])
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"store {store_id} has invalid recorded_at timestamp") from None
    seen_targets = set()
    for row in targets:
        if (not isinstance(row[0], str) or not row[0] or not isinstance(row[1], str) or not row[1]
                or (row[2] is not None and (type(row[2]) is not int or row[2] not in (0, 1)))):
            raise ValueError(f"store {store_id} has invalid target membership data")
        key = (row[0], row[1])
        if key in seen_targets:
            raise ValueError(f"store {store_id} has duplicate target identity")
        seen_targets.add(key)
    return attempts, facts, targets


def _validate_and_merge(histories, *, cutoff: datetime | str | None = None):
    """Merge disjoint partition histories and reject every ambiguous identity."""
    partition_histories: dict[str, tuple] = {}
    attempt_rows: dict[str, tuple] = {}
    fact_rows: dict[str, tuple] = {}
    target_rows: dict[tuple[str, str], tuple] = {}
    global_facts_by_attempt: dict[str, dict[int, tuple]] = defaultdict(dict)
    global_targets_by_coverage: dict[str, dict[str, tuple]] = defaultdict(dict)

    for store_id, history in histories:
        if history is None:
            continue
        attempts, facts, targets = history
        attempts_by_partition: dict[str, list[tuple]] = defaultdict(list)
        attempts_by_id = {}
        for row in attempts:
            attempt = coverage._attempt_from_row(row)
            if attempt.attempt_id in attempts_by_id:
                raise ValueError("duplicate attempt identity in a store snapshot")
            attempts_by_id[attempt.attempt_id] = row
            previous = attempt_rows.get(attempt.attempt_id)
            if previous is not None and previous != row:
                raise ValueError("conflicting global attempt identity")
            attempt_rows[attempt.attempt_id] = row
            attempts_by_partition[attempt.partition_id].append(row)

        facts_by_attempt: dict[str, list[tuple]] = defaultdict(list)
        facts_by_id = {}
        for row in facts:
            fact = coverage._fact_from_row(row)
            if fact.coverage_id in facts_by_id:
                raise ValueError("duplicate coverage identity in a store snapshot")
            facts_by_id[fact.coverage_id] = row
            previous = fact_rows.get(fact.coverage_id)
            if previous is not None and previous != row:
                raise ValueError("conflicting global coverage identity")
            fact_rows[fact.coverage_id] = row
            facts_by_attempt[fact.attempt_id].append(row)
            previous_state = global_facts_by_attempt[fact.attempt_id].get(fact.state_seq)
            if previous_state is not None and previous_state != row:
                raise ValueError("conflicting global coverage state identity")
            global_facts_by_attempt[fact.attempt_id][fact.state_seq] = row

        targets_by_coverage: dict[str, list[tuple]] = defaultdict(list)
        for row in targets:
            coverage_id, identity, _acquired = row
            key = (coverage_id, identity)
            previous = target_rows.get(key)
            if previous is not None and previous != row:
                raise ValueError("conflicting global target identity")
            target_rows[key] = row
            targets_by_coverage[coverage_id].append(row)
            global_targets_by_coverage[coverage_id][identity] = row

        if set(facts_by_attempt) - set(attempts_by_id):
            raise ValueError("coverage fact references an unknown attempt")
        if set(targets_by_coverage) - set(facts_by_id):
            raise ValueError("coverage target references an unknown fact")
        for row in facts:
            fact = coverage._fact_from_row(row)
            attempt_row = attempts_by_id.get(fact.attempt_id)
            if attempt_row is None:
                raise ValueError("coverage fact references an unknown attempt")
            attempt = coverage._attempt_from_row(attempt_row)
            if any(getattr(fact, field) != getattr(attempt, field) for field in (
                "logical_task_id", "attempt_id", "partition_id", "attempt_seq", "source_id",
                "product", "subject_kind", "subject_id", "query_spec_hash", "query_spec_json",
                "coverage_policy_version", "requested_from", "requested_until",
            )):
                raise ValueError("coverage fact conflicts with its attempt")

        for partition_id, rows in attempts_by_partition.items():
            rows = sorted(rows, key=lambda row: row[3])
            if [row[3] for row in rows] != list(range(1, len(rows) + 1)):
                raise ValueError("partition attempt history is incomplete or duplicated")
            partition_facts = tuple(
                fact_row
                for attempt_row in rows
                for fact_row in facts_by_attempt.get(attempt_row[1], ())
            )
            partition_targets = tuple(
                target_row
                for fact_row in partition_facts
                for target_row in targets_by_coverage.get(fact_row[0], ())
            )
            history_signature = (tuple(rows), partition_facts, partition_targets)
            previous = partition_histories.get(partition_id)
            if previous is not None and previous != history_signature:
                raise ValueError("conflicting partition history across store snapshots")
            partition_histories[partition_id] = history_signature

        for attempt_id, rows in facts_by_attempt.items():
            rows.sort(key=lambda row: row[5])
            if [row[5] for row in rows] != list(range(1, len(rows) + 1)):
                raise ValueError("coverage state history is incomplete or duplicated")
            times = [timefields.stored(row[23]) for row in rows]
            if any(later < earlier for earlier, later in zip(times, times[1:])):
                raise ValueError("coverage recorded_at timestamps must be nondecreasing")

    # An identity may repeat only as part of a byte-for-byte equal history above.
    # The signature check also catches partially copied histories of a partition.
    if cutoff is None:
        cutoff_time = None
    elif isinstance(cutoff, datetime):
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("coverage cutoff must be timezone-aware")
        cutoff_time = cutoff.astimezone(timezone.utc)
    else:
        cutoff_time = timefields.stored(cutoff)
    latest = []
    partition_attempts: dict[str, list[tuple]] = defaultdict(list)
    for row in attempt_rows.values():
        attempt = coverage._attempt_from_row(row)
        partition_attempts[attempt.partition_id].append(row)
    for rows in partition_attempts.values():
        selected = max(rows, key=lambda row: row[3])
        attempt = coverage._attempt_from_row(selected)
        states = global_facts_by_attempt.get(attempt.attempt_id, {})
        if cutoff_time is None:
            fact = states[max(states)] if states else None
        else:
            # Attempts have no trusted started_at field. Only a fact recorded by
            # cutoff proves the latest attempt was already observable then. A
            # later fact, or an attempt with no fact, cannot be silently
            # discarded in favor of an older successful retry.
            visible_states = [
                row for row in states.values()
                if timefields.stored(row[23]) <= cutoff_time
            ]
            fact = max(visible_states, key=lambda row: row[5]) if visible_states else None
        fact_obj = coverage._fact_from_row(fact) if fact is not None else None
        targets = ()
        if fact is not None:
            target_memberships = global_targets_by_coverage.get(fact[0], {})
            targets = tuple(
                coverage.CoverageTarget(identity=identity, acquired=None if row[2] is None else bool(row[2]))
                for identity, row in sorted(target_memberships.items())
            )
        latest.append(coverage.CoverageSnapshot(attempt, fact_obj, targets))
    return latest


def _canonical_value(value: Any):
    if isinstance(value, Mapping):
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_value(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(
            item, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ))
    return value


def _utc_identity_time(value: str) -> str:
    return timefields.stored(value).astimezone(timezone.utc).isoformat(timespec="microseconds")


def coverage_identity_sha256(
    requirements: Sequence[CoverageRequirement], plan: CoveragePlan,
) -> tuple[str, str]:
    """Return canonical hashes that bind readiness to its exact declaration."""
    requirement_rows = []
    for req in sorted(requirements, key=lambda item: item.requirement_id):
        requirement_rows.append({
            "requirement_id": req.requirement_id,
            "session_date": req.session_date,
            "ticker": req.ticker,
            "category": req.category,
            "scope_kind": req.scope_kind,
            "scope_id": req.scope_id,
            "subject_kind": req.subject_kind,
            "subject_id": req.subject_id,
            "product": req.product,
            "requested_from": _utc_identity_time(req.requested_from),
            "requested_until": _utc_identity_time(req.requested_until),
            "query_filters": _canonical_value(req.query_filters),
            "source_ids": sorted(req.source_ids),
            "source_policy": req.source_policy,
            "session_ids": sorted(req.session_ids),
            "feed_id": req.feed_id,
        })
    plan_payload = {
        "partitions": [
            {
                "partition_id": part.partition_id,
                "source_id": part.source_id,
                "product": part.product,
                "subject_kind": part.subject_kind,
                "subject_id": part.subject_id,
                "requested_from": _utc_identity_time(part.requested_from),
                "requested_until": _utc_identity_time(part.requested_until),
                "query_filters": _canonical_value(part.query_filters),
                "coverage_policy_version": part.coverage_policy_version,
                "session_ids": sorted(part.session_ids),
            }
            for part in sorted(plan.partitions, key=lambda item: item.partition_id)
        ],
        "capabilities": {
            source_id: {
                "products": sorted(cap.products),
                "subject_kinds": sorted(cap.subject_kinds),
            }
            for source_id, cap in sorted(plan.capabilities.items())
        },
        "bindings": {
            source_id: {"feed_id": binding.feed_id}
            for source_id, binding in sorted(plan.bindings.items())
        },
        "category_mapping": _canonical_value(plan.category_mapping),
    }
    if plan.internal_category_mapping is not None:
        plan_payload["internal_category_mapping"] = _canonical_value(plan.internal_category_mapping)

    def digest(payload: Any) -> str:
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    return digest(requirement_rows), digest(plan_payload)




def _public_requirement(req: CoverageRequirement, result: dict) -> dict:
    source_status = result["status"]
    if source_status == "DISABLED":
        status, reason = "NOT_FETCHED", "DISABLED"
    elif source_status == "PENDING":
        status, reason = "NOT_FETCHED", None
    elif source_status == "ERROR":
        status, reason = "ERROR", "ERROR"
    elif source_status in {"OK", "EMPTY", "NOT_FETCHED"}:
        status, reason = source_status, None
    else:
        raise ValueError("coverage evaluator returned an unsupported public status")
    complete = bool(result["complete"])
    return {
        "requirement_id": req.requirement_id,
        "session_date": req.session_date,
        "ticker": req.ticker,
        "category": req.category,
        "scope_kind": req.scope_kind,
        "scope_id": req.scope_id,
        "product": req.product,
        "requested_from": req.requested_from,
        "requested_until": req.requested_until,
        "status": status,
        "reason": reason,
        "complete": complete,
        "missing_requirement_ids": [] if complete else [req.requirement_id],
    }


def _aggregate_public(items: Sequence[dict]) -> tuple[str, str | None, bool, list[str]]:
    complete = all(item["complete"] for item in items)
    missing = [
        requirement_id
        for item in items
        for requirement_id in item["missing_requirement_ids"]
    ]
    statuses = {item["status"] for item in items}
    if complete:
        status = "OK" if "OK" in statuses else "EMPTY"
        reason = None
    elif "ERROR" in statuses:
        status, reason = "ERROR", "ERROR"
    elif "OK" in statuses:
        status, reason = "OK", None
    elif "EMPTY" in statuses:
        status, reason = "EMPTY", None
    else:
        status = "NOT_FETCHED"
        reason = "DISABLED" if any(item["reason"] == "DISABLED" for item in items) else None
    return status, reason, complete, missing


def _public_categories(requirements: Sequence[CoverageRequirement], raw_results: Sequence[dict]) -> list[dict]:
    categories: dict[tuple[str, str, str], dict] = {}
    for requirement, raw in zip(requirements, raw_results):
        category_key = (requirement.session_date, requirement.ticker, requirement.category)
        category = categories.setdefault(category_key, {
            "session_date": requirement.session_date,
            "ticker": requirement.ticker,
            "category": requirement.category,
            "_requirements": [],
            "_products": {},
        })
        public = _public_requirement(requirement, raw)
        category["_requirements"].append(public)
        category["_products"].setdefault(requirement.product, []).append(public)

    output = []
    for category in categories.values():
        status, reason, complete, missing = _aggregate_public(category["_requirements"])
        products = []
        for product, product_requirements in category["_products"].items():
            product_status, product_reason, product_complete, product_missing = _aggregate_public(product_requirements)
            products.append({
                "product": product,
                "status": product_status,
                "reason": product_reason,
                "complete": product_complete,
                "missing_requirement_ids": product_missing,
                "requirements": product_requirements,
            })
        output.append({
            "session_date": category["session_date"],
            "ticker": category["ticker"],
            "category": category["category"],
            "status": status,
            "reason": reason,
            "complete": complete,
            "missing_requirement_ids": missing,
            "products": products,
        })
    return output


def evaluate_snapshots(
    requirements: Sequence[CoverageRequirement],
    plan: CoveragePlan,
    verified_stores: VerifiedStores,
    *,
    cutoff: datetime | str | None = None,
) -> dict:
    """Evaluate only declared G1 coverage facts from verified immutable stores.

    The audit contains versioned hashes of the requirements and plan. Callers
    must retain those declarations, or a complete canonical declaration file,
    to reproduce the hashes; hashes alone cannot reconstruct their inputs.
    """
    requirements = tuple(requirements)
    if not requirements:
        raise ValueError("at least one coverage requirement is required")
    if not isinstance(plan, CoveragePlan):
        raise ValueError("an explicit CoveragePlan is required")
    if plan.internal_category_mapping is not None:
        raise ValueError("internal O1 routes are not supported by public snapshot evaluation")
    if not isinstance(verified_stores, VerifiedStores):
        raise ValueError("manifest-verified stores are required")
    validate_verified_stores(verified_stores)
    for req in requirements:
        if not isinstance(req, CoverageRequirement):
            raise ValueError("requirements must be validated CoverageRequirement values")

    histories = []
    for store_id in sorted(verified_stores.stores):
        store = verified_stores.stores[store_id]
        try:
            with readonly_snapshot(verified_stores, store) as conn:
                histories.append((store_id, _history(conn, store_id)))
        except sqlite3.Error as exc:
            raise ValueError(f"store {store_id} is unreadable or has an invalid schema") from exc
    snapshots = _validate_and_merge(histories, cutoff=cutoff)
    results = evaluate_coverage(requirements, plan, snapshots)
    validate_verified_stores(verified_stores)
    if not snapshots:
        for result in results:
            if result["status"] == "PENDING":
                result["status"] = "NOT_FETCHED"
                for subresult in result["subrequirements"]:
                    if subresult["status"] == "PENDING":
                        subresult["status"] = "NOT_FETCHED"

    missing = [result["requirement_id"] for result in results if not result["complete"]]
    completed = len(results) - len(missing)
    public_results = _public_categories(requirements, results)
    snapshot_ids = {
        store_id: store.sha256
        for store_id, store in sorted(verified_stores.stores.items())
    }
    requirements_sha256, plan_sha256 = coverage_identity_sha256(requirements, plan)
    missing_optional_store_ids = list(verified_stores.missing_optional_store_ids)
    return {
        "results": public_results,
        "progress": {
            "completed_requirements": completed,
            "total_declared_requirements": len(requirements),
        },
        "audit": {
            "manifest_sha256": verified_stores.manifest_sha256,
            "data_snapshot_ids": snapshot_ids,
            "missing_optional_store_ids": missing_optional_store_ids,
            "required_store_ids": list(verified_stores.required_store_ids),
            "store_count": len(snapshot_ids),
            "requirement_count": len(requirements),
            "coverage_identity_format": _COVERAGE_IDENTITY_FORMAT,
            "coverage_identity_version": _COVERAGE_IDENTITY_VERSION,
            "coverage_requirements_sha256": requirements_sha256,
            "coverage_plan_sha256": plan_sha256,
            "missing_requirement_ids": missing,
            "read_only": True,
        },
    }


def _evaluate_o1_live_connections(
    requirements: Sequence[CoverageRequirement],
    plan: CoveragePlan,
    connections: Mapping[str, sqlite3.Connection],
    *,
    cutoff: datetime | str,
) -> dict:
    """Apply G4 history validation to caller-owned live read-only transactions.

    Unlike :func:`evaluate_snapshots`, this path does not claim immutable file
    hashes. Every connection must already be query-only and inside a read
    transaction; histories use the same G4 schema validator, latest-attempt,
    cutoff, merge, and requirement evaluator as pinned snapshots.
    """
    requirements = tuple(requirements)
    if not requirements:
        raise ValueError("at least one coverage requirement is required")
    if not isinstance(plan, CoveragePlan):
        raise ValueError("an explicit CoveragePlan is required")
    if not isinstance(connections, Mapping) or not connections:
        raise ValueError("live store connections are required")
    if any(not isinstance(store_id, str) or not store_id.strip() for store_id in connections):
        raise ValueError("live store identities must be nonempty strings")
    if isinstance(cutoff, datetime):
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("coverage cutoff must be timezone-aware")
        cutoff_value = cutoff.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    else:
        cutoff_value = timefields.stored(cutoff).astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    histories = []
    read_started_at = {}
    for store_id, conn in sorted(connections.items()):
        if not isinstance(conn, sqlite3.Connection):
            raise ValueError("live store entries must be SQLite connections")
        query_only = conn.execute("PRAGMA query_only").fetchone()
        if not query_only or query_only[0] != 1 or not conn.in_transaction:
            raise ValueError("live stores must be query-only connections in read transactions")
        read_started_at[store_id] = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        histories.append((store_id, _history(conn, store_id)))
    snapshots = _validate_and_merge(histories, cutoff=cutoff_value)
    stores_by_partition: dict[str, set[str]] = defaultdict(set)
    for store_id, history in histories:
        if history is None:
            continue
        for row in history[0]:
            stores_by_partition[row[2]].add(store_id)
    results = _evaluate_coverage(requirements, plan, snapshots, allow_internal=True)
    if not snapshots:
        for result in results:
            if result["status"] == "PENDING":
                result["status"] = "NOT_FETCHED"
                for subresult in result["subrequirements"]:
                    if subresult["status"] == "PENDING":
                        subresult["status"] = "NOT_FETCHED"
    missing = [result["requirement_id"] for result in results if not result["complete"]]
    completed = len(results) - len(missing)
    requirements_sha256, plan_sha256 = coverage_identity_sha256(requirements, plan)
    return {
        "results": _public_categories(requirements, results),
        "progress": {"completed_requirements": completed,
                     "total_declared_requirements": len(requirements)},
        "audit": {
            "source": "registered-live-read-only-transactions",
            "store_ids": list(sorted(connections)),
            "cutoff": cutoff_value,
            "snapshot_model": "independent-live-store-read-transactions",
            "store_read_started_at": read_started_at,
            "coverage_identity_format": _COVERAGE_IDENTITY_FORMAT,
            "coverage_identity_version": _COVERAGE_IDENTITY_VERSION,
            "coverage_requirements_sha256": requirements_sha256,
            "coverage_plan_sha256": plan_sha256,
            "coverage_evidence": [{
                "store_ids": sorted(stores_by_partition.get(snapshot.attempt.partition_id, ())),
                "partition_id": snapshot.attempt.partition_id,
                "attempt_id": snapshot.attempt.attempt_id,
                "attempt_seq": snapshot.attempt.attempt_seq,
                "query_spec_hash": snapshot.attempt.query_spec_hash,
                "coverage_policy_version": snapshot.attempt.coverage_policy_version,
                "fact_id": snapshot.fact.coverage_id if snapshot.fact else None,
                "fact_state_seq": snapshot.fact.state_seq if snapshot.fact else None,
                "status": snapshot.fact.status if snapshot.fact else None,
                "recorded_at": snapshot.fact.recorded_at if snapshot.fact else None,
                "coverage_from": snapshot.fact.coverage_from if snapshot.fact else None,
                "coverage_until": snapshot.fact.coverage_until if snapshot.fact else None,
                "enumeration_complete": snapshot.fact.enumeration_complete if snapshot.fact else None,
                "targets": sorted((target.identity, target.acquired) for target in snapshot.targets),
            } for snapshot in sorted(snapshots, key=lambda item: (
                item.attempt.partition_id, item.attempt.attempt_seq))],
            "missing_requirement_ids": missing,
            "read_only": True,
        },
    }


__all__ = ["coverage_identity_sha256", "evaluate_snapshots"]
