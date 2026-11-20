"""Current role-aware completeness over an explicitly declared collection plan.

This is the authoritative evaluator for immutable ``coverage`` snapshots.
The older ``gaps.GapLedger.evaluate`` projection consumes mutable, coarse
GapRecord rows and must not be used to claim current coverage completeness.
This module is pure: callers provide the current requirements, source registry
capabilities/bindings, exact planned partitions, and frozen G1 snapshots.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from catalyst_data.ingestion import coverage, source_categories
from catalyst_data.ingestion.time import timefields

_PRODUCTS = {"headlines", "full_text", "bars", "search"}
_SUBJECTS = {"ticker", "instrument", "feed", "query"}
_SCOPES = {"target", "peer", "supplementary", "reference"}
_STATUSES = {"OK", "EMPTY", "ERROR", "NOT_FETCHED"}


def _time(value: str, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a timezone-aware timestamp")
    try:
        return timefields.stored(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a timezone-aware timestamp") from None


def _filters(value: Mapping[str, Any], name: str) -> dict[str, frozenset[Any]]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be equality filters")
    result = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key or key.startswith("$"):
            raise ValueError(f"{name} contains an unknown filter operator")
        values = raw if isinstance(raw, (list, tuple, set, frozenset)) else (raw,)
        if any(not (item is None or type(item) in (str, int, float, bool)) for item in values):
            raise ValueError(f"{name} values must be safe equality values")
        if any(isinstance(item, float) and (item != item or abs(item) == float("inf")) for item in values):
            raise ValueError(f"{name} values must be finite")
        try:
            result[key] = frozenset(values)
        except TypeError:
            raise ValueError(f"{name} values must be equality values") from None
    return result


def _freeze_filters(value: Mapping[str, Any], name: str) -> Mapping[str, tuple[Any, ...]]:
    _, encoded = coverage._safe_query_spec(value)
    decoded = json.loads(encoded)
    immutable = {key: tuple(item) if isinstance(item, list) else item
                 for key, item in decoded.items()}
    return MappingProxyType(immutable)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class CoverageRequirement:
    requirement_id: str
    session_date: str
    ticker: str
    category: str
    scope_kind: str
    scope_id: str
    subject_kind: str
    subject_id: str
    product: str
    requested_from: str
    requested_until: str
    query_filters: Mapping[str, Any] = field(default_factory=dict)
    source_ids: tuple[str, ...] = ()
    source_policy: str = "all"
    session_ids: tuple[str, ...] = ()
    feed_id: str | None = None

    def __post_init__(self):
        for name in ("requirement_id", "session_date", "ticker", "category", "scope_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be non-empty")
        if self.scope_kind not in _SCOPES:
            raise ValueError("unknown requirement scope kind")
        if self.subject_kind not in _SUBJECTS:
            raise ValueError("unknown required subject kind")
        if self.subject_kind in {"ticker", "instrument"} and self.subject_id != self.scope_id:
            raise ValueError("ticker and instrument requirement subjects must equal scope_id")
        if not isinstance(self.subject_id, str) or not self.subject_id.strip():
            raise ValueError("subject_id must be nonempty")
        if self.product not in _PRODUCTS:
            raise ValueError("unknown required product")
        if self.feed_id is not None and (self.subject_kind != "feed" or self.feed_id != self.subject_id):
            raise ValueError("explicit feed requirement must name its feed subject")
        if self.source_policy not in {"all", "any"}:
            raise ValueError("source_policy must be all or any")
        if not isinstance(self.source_ids, tuple) or not self.source_ids:
            raise ValueError("source_ids must be a nonempty tuple")
        if any(not isinstance(source_id, str) or not source_id.strip() for source_id in self.source_ids):
            raise ValueError("source_ids must contain nonempty source IDs")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("duplicate source_id")
        if _time(self.requested_from, "requested_from") >= _time(self.requested_until, "requested_until"):
            raise ValueError("requirement interval must be nonempty")
        object.__setattr__(self, "query_filters", _freeze_filters(self.query_filters, "requirement filters"))
        object.__setattr__(self, "source_ids", tuple(self.source_ids))
        object.__setattr__(self, "session_ids", tuple(self.session_ids))
        if self.product == "bars" and (not self.session_ids or len(set(self.session_ids)) != len(self.session_ids)):
            raise ValueError("bars requirements need an explicit unique session set")
        if any(not isinstance(item, str) or not item for item in self.session_ids):
            raise ValueError("session_ids must be nonempty strings")
        if self.product != "bars" and self.session_ids:
            raise ValueError("session_ids are reserved for bars requirements")


@dataclass(frozen=True)
class SourceCapability:
    products: frozenset[str]
    subject_kinds: frozenset[str]

    def __post_init__(self):
        object.__setattr__(self, "products", frozenset(self.products))
        object.__setattr__(self, "subject_kinds", frozenset(self.subject_kinds))
        if not self.products or not self.products <= _PRODUCTS:
            raise ValueError("source capability has unknown or empty products")
        if not self.subject_kinds or not self.subject_kinds <= _SUBJECTS:
            raise ValueError("source capability has unknown or empty subject kinds")


@dataclass(frozen=True)
class SourceBinding:
    """An explicit configured feed identity for a source."""
    feed_id: str

    def __post_init__(self):
        if not isinstance(self.feed_id, str) or not self.feed_id.strip():
            raise ValueError("feed binding needs a feed_id")


@dataclass(frozen=True)
class PlannedPartition:
    partition_id: str
    source_id: str
    product: str
    subject_kind: str
    subject_id: str
    requested_from: str
    requested_until: str
    query_filters: Mapping[str, Any]
    coverage_policy_version: str
    session_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if not all(isinstance(getattr(self, name), str) and getattr(self, name).strip()
                   for name in ("partition_id", "source_id", "product", "subject_kind", "subject_id", "coverage_policy_version")):
            raise ValueError("planned partition identity fields must be nonempty")
        if self.product not in _PRODUCTS or self.subject_kind not in _SUBJECTS:
            raise ValueError("planned partition has unknown product or subject kind")
        if _time(self.requested_from, "planned requested_from") >= _time(self.requested_until, "planned requested_until"):
            raise ValueError("planned partition interval must be nonempty")
        object.__setattr__(self, "query_filters", _freeze_filters(self.query_filters, "plan filters"))
        object.__setattr__(self, "session_ids", tuple(self.session_ids))
        if any(not isinstance(item, str) or not item for item in self.session_ids):
            raise ValueError("planned session_ids must be nonempty strings")
        if self.product == "bars" and (self.subject_kind != "instrument" or not self.session_ids):
            raise ValueError("bars partitions require an instrument and explicit sessions")


@dataclass(frozen=True)
class CoveragePlan:
    """Explicit current expected partitions and internal registry configuration."""
    partitions: tuple[PlannedPartition, ...]
    capabilities: Mapping[str, SourceCapability]
    bindings: Mapping[str, SourceBinding]
    category_mapping: Mapping[str, Any] | None = None
    internal_category_mapping: Mapping[str, str] | None = None

    def __post_init__(self):
        if not isinstance(self.partitions, tuple):
            raise ValueError("plan partitions must be a tuple")
        object.__setattr__(self, "capabilities", MappingProxyType(dict(self.capabilities)))
        object.__setattr__(self, "bindings", MappingProxyType(dict(self.bindings)))
        if self.internal_category_mapping is not None:
            internal = dict(self.internal_category_mapping)
            if any(not isinstance(source, str) or not isinstance(category, str) or
                   not category.startswith("o1_") for source, category in internal.items()):
                raise ValueError("internal coverage routes must use explicit o1_ categories")
            object.__setattr__(self, "internal_category_mapping", MappingProxyType(internal))
        if self.category_mapping is not None:
            object.__setattr__(self, "category_mapping", _deep_freeze(_deep_thaw(self.category_mapping)))
        keys = [_partition_key(p) for p in self.partitions]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate planned partition")
        if any(p.partition_id != expected_partition_id(p) for p in self.partitions):
            raise ValueError("planned partition ID does not match its declared query plan")
        registry = source_categories.source_registry.load()
        if self.category_mapping is not None:
            source_categories.validate(_deep_thaw(self.category_mapping), registry)
        known = {row["id"] for row in registry["sources"]}
        if self.internal_category_mapping is not None and not set(self.internal_category_mapping) <= known:
            raise ValueError("internal coverage route references unknown source")
        if self.internal_category_mapping is not None:
            public_mapping = (_deep_thaw(self.category_mapping) if self.category_mapping is not None
                              else source_categories.load(registry=registry))
            registered_sources = {row["id"]: row for row in registry["sources"]}
            for source_id, category in self.internal_category_mapping.items():
                source = registered_sources[source_id]
                route = public_mapping["sources"][source_id]
                if "excluded" not in route:
                    raise ValueError("internal coverage routes are reserved for publicly excluded sources")
                if source.get("category") == "regulatory":
                    expected = "o1_regulatory"
                elif source.get("category") == "calendar":
                    expected = "o1_calendar"
                elif source.get("store") == "market_archive" and source.get("key") == "finra/*":
                    expected = "o1_finra"
                else:
                    expected = None
                if category != expected:
                    raise ValueError("internal coverage route conflicts with registered O1 source metadata")
        for source_id, cap in self.capabilities.items():
            if source_id not in known:
                raise ValueError("capability references unknown registry source")
            if not isinstance(cap, SourceCapability):
                raise ValueError("source capability configuration is required")
        for source_id, binding in self.bindings.items():
            if source_id not in known or not isinstance(binding, SourceBinding):
                raise ValueError("feed binding references unknown source or invalid binding")
            cap = self.capabilities.get(source_id)
            if cap is None or "feed" not in cap.subject_kinds:
                raise ValueError("feed binding requires an explicit feed source capability")
        for p in self.partitions:
            cap = self.capabilities.get(p.source_id)
            if cap is None:
                raise ValueError(f"missing source capability for {p.source_id}")
            if p.product not in cap.products or p.subject_kind not in cap.subject_kinds:
                raise ValueError("planned partition uses unsupported product or subject kind")

    def validate_requirements(self, requirements: Sequence[CoverageRequirement]) -> None:
        """Validate all declared source/category/capability combinations before collection."""
        validate_requirements(requirements, self)


def _partition_key(p: PlannedPartition) -> tuple:
    return (p.partition_id,)


def _filter_covers(collection: Mapping[str, Any], request: Mapping[str, Any],
                   session_ids: Sequence[str] = ()) -> bool:
    have, need = _filters(collection, "collection filters"), _filters(request, "requirement filters")
    if session_ids:
        session_keys = {"session", "session_id", "session_ids", "session_date"}
        collection_sets = [have.pop(key) for key in session_keys if key in have]
        if collection_sets:
            collection_sessions = set().union(*collection_sets)
            if not set(session_ids) <= collection_sessions:
                return False
        for key in session_keys:
            need.pop(key, None)
    return all(key in need and need[key] <= collection_values for key, collection_values in have.items())


def _query_sessions(filters: Mapping[str, Any]) -> frozenset[str]:
    keys = {"session", "session_id", "session_ids", "session_date"}
    found = [set(values) for key, values in _filters(filters, "session filters").items() if key in keys]
    if not found or any(values != found[0] for values in found[1:]):
        return frozenset()
    values = found[0]
    if not values or any(not isinstance(value, str) or not value for value in values):
        return frozenset()
    return frozenset(values)


def _compatible(snapshot: coverage.CoverageSnapshot, requirement: CoverageRequirement,
                partition: PlannedPartition, plan: CoveragePlan) -> bool:
    attempt, fact = snapshot.attempt, snapshot.fact
    if fact is None or fact.status not in _STATUSES:
        return False
    if (attempt.partition_id != fact.partition_id or attempt.attempt_id != fact.attempt_id or
            attempt.attempt_seq != fact.attempt_seq or attempt.source_id != fact.source_id or
            attempt.product != fact.product or attempt.subject_kind != fact.subject_kind or
            attempt.subject_id != fact.subject_id or attempt.query_spec_hash != fact.query_spec_hash or
            attempt.query_spec_json != fact.query_spec_json or
            attempt.coverage_policy_version != fact.coverage_policy_version):
        return False
    if (attempt.source_id != partition.source_id or attempt.product != requirement.product or
            attempt.partition_id != partition.partition_id or
            attempt.product != partition.product or attempt.subject_kind != partition.subject_kind or
            attempt.subject_id != partition.subject_id or
            _time(attempt.requested_from, "attempt requested_from") != _time(partition.requested_from, "plan requested_from") or
            _time(attempt.requested_until, "attempt requested_until") != _time(partition.requested_until, "plan requested_until") or
            attempt.coverage_policy_version != partition.coverage_policy_version):
        return False
    planned_hash, planned_json = coverage._safe_query_spec(partition.query_filters)
    if attempt.query_spec_hash != planned_hash or attempt.query_spec_json != planned_json:
        return False
    explicit_sessions = partition.session_ids if requirement.product == "bars" else ()
    if not _filter_covers(json.loads(attempt.query_spec_json), requirement.query_filters,
                          explicit_sessions):
        return False
    if requirement.product == "bars":
        if attempt.subject_kind != "instrument":
            return False
        if not requirement.session_ids or not partition.session_ids:
            return False
        actual_sessions = _query_sessions(json.loads(attempt.query_spec_json))
        if not actual_sessions or actual_sessions != set(partition.session_ids):
            return False
    if attempt.subject_kind != requirement.subject_kind or attempt.subject_id != requirement.subject_id:
        return False
    if attempt.subject_kind == "feed":
        binding = plan.bindings.get(attempt.source_id)
        if binding is not None and binding.feed_id != attempt.subject_id:
            return False
        if requirement.feed_id is not None and requirement.feed_id != attempt.subject_id:
            return False
    return True


def _latest(snapshots: Sequence[coverage.CoverageSnapshot]) -> dict[str, coverage.CoverageSnapshot]:
    by_partition = {}
    seen_attempts = set()
    for snap in snapshots:
        if snap.attempt.partition_id in by_partition or snap.attempt.attempt_id in seen_attempts:
            raise ValueError("duplicate snapshot partition or attempt identity")
        by_partition[snap.attempt.partition_id] = snap
        seen_attempts.add(snap.attempt.attempt_id)
    return by_partition


def _partition_result(snap: coverage.CoverageSnapshot | None, req: CoverageRequirement,
                      part: PlannedPartition, plan: CoveragePlan) -> tuple[str, bool, tuple[datetime, datetime] | None]:
    if snap is None or not _compatible(snap, req, part, plan) or snap.fact is None:
        return "PENDING", False, None
    fact = snap.fact
    if fact.status == "NOT_FETCHED":
        return ("DISABLED" if fact.reason == "DISABLED" else "PENDING"), False, None
    if fact.status not in {"OK", "EMPTY"}:
        return fact.status, False, None
    if fact.enumeration_complete is not True or not fact.coverage_from or not fact.coverage_until:
        return fact.status, False, None
    actual_interval = (_time(fact.coverage_from, "coverage_from"),
                       _time(fact.coverage_until, "coverage_until"))
    plan_interval = (_time(part.requested_from, "plan requested_from"),
                     _time(part.requested_until, "plan requested_until"))
    if actual_interval[0] >= actual_interval[1] or actual_interval[0] < plan_interval[0] or actual_interval[1] > plan_interval[1]:
        return fact.status, False, None
    if req.product == "full_text":
        if fact.expected_target_count is None or fact.acquired_target_count is None:
            return fact.status, False, None
        if fact.status == "OK" and fact.expected_target_count == 0:
            return fact.status, False, None
        identities = {target.identity for target in snap.targets}
        if len(identities) != len(snap.targets) or len(identities) != fact.expected_target_count:
            return fact.status, False, None
        if fact.status == "OK" and (fact.acquired_target_count != fact.expected_target_count or
                                     any(target.acquired is not True for target in snap.targets)):
            return fact.status, False, None
        if fact.status == "EMPTY" and fact.acquired_target_count != 0:
            return fact.status, False, None
        if fact.status == "EMPTY" and (fact.expected_target_count != 0 or snap.targets):
            return fact.status, False, None
    return fact.status, True, actual_interval


def _children_cover(parts: Sequence[PlannedPartition], start: str, end: str) -> bool:
    intervals = [(_time(p.requested_from, "plan start"), _time(p.requested_until, "plan end")) for p in parts]
    return _intervals_cover(intervals, start, end)


def _intervals_cover(intervals: Sequence[tuple[datetime, datetime]], start: str, end: str) -> bool:
    intervals = sorted(intervals)
    cursor = _time(start, "requirement start")
    upper = _time(end, "requirement end")
    for left, right in intervals:
        if right <= cursor or left > cursor:
            continue
        cursor = max(cursor, right)
        if cursor >= upper:
            return True
    return False


def validate_requirements(requirements: Sequence[CoverageRequirement], plan: CoveragePlan) -> None:
    """Reject impossible caller/source combinations independently of snapshots."""
    ids = [requirement.requirement_id for requirement in requirements]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate requirement_id")
    for req in requirements:
        for source_id in req.source_ids:
            if plan.internal_category_mapping is not None and source_id in plan.internal_category_mapping:
                category = plan.internal_category_mapping[source_id]
            else:
                try:
                    category = source_categories.category_for(
                        source_id, scope=None if req.scope_kind == "reference" else req.scope_kind,
                        mapping=_deep_thaw(plan.category_mapping) if plan.category_mapping is not None else None,
                    )
                except ValueError:
                    raise ValueError("requirement source is not a public registry source for this scope") from None
            if category != req.category:
                raise ValueError("requirement category conflicts with registry route")
            cap = plan.capabilities.get(source_id)
            if cap is None:
                raise ValueError(f"missing source capability for {source_id}")
            if req.product not in cap.products:
                raise ValueError("requirement uses unsupported product")
            if req.subject_kind not in cap.subject_kinds:
                raise ValueError("requirement uses unsupported subject kind")
            if req.product == "bars" and req.subject_kind != "instrument":
                raise ValueError("bars requirements need an instrument subject")
            binding = plan.bindings.get(source_id)
            if req.subject_kind == "feed" and binding is not None and binding.feed_id != req.subject_id:
                raise ValueError("configured feed binding conflicts with explicit feed requirement")
            participating = [part for part in plan.partitions
                             if part.source_id == source_id and part.product == req.product
                             and part.subject_kind == req.subject_kind and part.subject_id == req.subject_id
                             and _time(part.requested_from, "plan start") < _time(req.requested_until, "requirement end")
                             and _time(part.requested_until, "plan end") > _time(req.requested_from, "requirement start")]
            if len({part.coverage_policy_version for part in participating}) > 1:
                raise ValueError("mixed coverage policy versions among participating partitions")


def evaluate_coverage(requirements: Sequence[CoverageRequirement], plan: CoveragePlan,
                      snapshots: Sequence[coverage.CoverageSnapshot]) -> list[dict[str, Any]]:
    """Evaluate each caller request against only its declared current plan."""
    if plan.internal_category_mapping is not None:
        raise ValueError("internal O1 routes are reserved for the private O1 evaluator")
    return _evaluate_coverage(requirements, plan, snapshots, allow_internal=False)


def _evaluate_coverage(requirements: Sequence[CoverageRequirement], plan: CoveragePlan,
                       snapshots: Sequence[coverage.CoverageSnapshot], *,
                       allow_internal: bool) -> list[dict[str, Any]]:
    if plan.internal_category_mapping is not None and not allow_internal:
        raise ValueError("internal O1 routes are reserved for the private O1 evaluator")
    validate_requirements(requirements, plan)
    latest = _latest(snapshots)
    results = []
    mapping = plan.category_mapping
    for req in requirements:
        mapped_scope = req.scope_kind
        outcomes = []
        for source_id in req.source_ids:
            cap = plan.capabilities[source_id]
            candidate_parts = [p for p in plan.partitions
                               if p.source_id == source_id and p.product == req.product
                               and p.subject_kind == req.subject_kind and p.subject_id == req.subject_id
                               and _time(p.requested_from, "plan start") < _time(req.requested_until, "requirement end")
                               and _time(p.requested_until, "plan end") > _time(req.requested_from, "requirement start")]
            if not candidate_parts:
                outcomes.append(("NOT_FETCHED", False))
                continue
            if any(p.subject_kind not in cap.subject_kinds for p in candidate_parts):
                raise ValueError("requirement uses unsupported subject kind")
            if req.product not in cap.products:
                raise ValueError("requirement uses unsupported product")
            by_id = {part.partition_id: latest.get(part.partition_id) for part in candidate_parts}
            part_results = [_partition_result(by_id[p.partition_id], req, p, plan) for p in candidate_parts]
            statuses = [status for status, _, _ in part_results]
            complete_window = _children_cover(candidate_parts, req.requested_from, req.requested_until)
            actual_intervals = [interval for _, complete, interval in part_results if complete and interval is not None]
            covered_sessions = set().union(*(set(part.session_ids)
                                             for part, (_, complete, _) in zip(candidate_parts, part_results)
                                             if complete))
            source_complete = (complete_window and all(complete for _, complete, _ in part_results)
                               and _intervals_cover(actual_intervals, req.requested_from, req.requested_until)
                               and (req.product != "bars" or set(req.session_ids) <= covered_sessions))
            if source_complete:
                status = "OK" if "OK" in statuses else "EMPTY"
                outcomes.append((status, True))
            elif "ERROR" in statuses:
                outcomes.append(("ERROR", False))
            elif "OK" in statuses:
                outcomes.append(("OK", False))
            elif "EMPTY" in statuses:
                outcomes.append(("EMPTY", False))
            elif "DISABLED" in statuses:
                outcomes.append(("DISABLED", False))
            elif "PENDING" in statuses:
                outcomes.append(("PENDING", False))
            else:
                outcomes.append(("NOT_FETCHED", False))
        statuses, complete_flags = zip(*outcomes)
        if req.source_policy == "all":
            complete = all(complete_flags)
            status = "OK" if complete and "OK" in statuses else "EMPTY" if complete else next((s for s in ("ERROR", "DISABLED", "OK", "EMPTY", "PENDING") if s in statuses), "NOT_FETCHED")
        else:
            complete = any(c and s == "OK" for s, c in outcomes) or all(c and s == "EMPTY" for s, c in outcomes)
            status = "OK" if any(c and s == "OK" for s, c in outcomes) else "EMPTY" if complete else next((s for s in ("ERROR", "DISABLED", "OK", "EMPTY", "PENDING") if s in statuses), "NOT_FETCHED")
        results.append({
            "requirement_id": req.requirement_id,
            "missing_requirement_ids": [] if complete else [req.requirement_id],
            "session_date": req.session_date, "ticker": req.ticker, "category": req.category,
            "scope_kind": req.scope_kind, "scope_id": req.scope_id, "product": req.product,
            "subrequirements": [{"status": s, "complete": c} for s, c in outcomes],
            "status": status, "complete": complete,
        })
    return results


def expected_partition_id(part: PlannedPartition) -> str:
    """Compute G1's stable ID from the plan declaration, before collection starts."""
    query_hash, query_json = coverage._safe_query_spec(part.query_filters)
    begin = coverage._timestamp(part.requested_from, "requested_from")
    end = coverage._timestamp(part.requested_until, "requested_until")
    identity = (part.source_id, part.product, part.subject_kind, part.subject_id, query_hash,
                query_json, part.coverage_policy_version, begin, end)
    return hashlib.sha256(json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
