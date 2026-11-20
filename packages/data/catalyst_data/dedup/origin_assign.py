"""Assign stable origins using confirmed release, publisher, and issuer evidence."""
from __future__ import annotations

import json
import re
import sqlite3

from catalyst_data.dedup import relations
from catalyst_data.dedup.publisher_registry import (
    PUBLISHER_REGISTRY,
    PublisherEntry,
    registry_sha256,
)
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage.atomic import atomic_savepoint


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a nonempty trimmed value")
    return value


def _time(value: str) -> str:
    _text(value, "at")
    try:
        return timefields.stamp(timefields.stored(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("at must be a timezone-aware timestamp") from exc


_FINGERPRINT_VERSION = r"[A-Za-z0-9._-]+"
SEC_PROVIDERS = ("sec", "daily:sec")
DERIVATIVE_RULE_VERSION = "d1-rules-v3"


def _valid_origin(origin: str) -> bool:
    return bool(re.fullmatch(r"release:(?!fp:)[^:]+:.+", origin)
                or re.fullmatch(rf"release:fp:{_FINGERPRINT_VERSION}:[0-9a-f]{{8,64}}", origin)
                or re.fullmatch(r"pub:[a-z0-9_-]+", origin)
                or re.fullmatch(r"issuer:cik:[0-9]{10}", origin))


def _origin_class(origin: str) -> str:
    # A fingerprint is its own priority, so it never merges with (or replaces) a confirmed release id.
    if origin.startswith("release:fp:"):
        return "release:fp"
    if origin.startswith("release:"):
        return "release"
    if origin.startswith("pub:"):
        return "pub"
    if origin.startswith("issuer:cik:"):
        return "issuer:cik"
    raise ValueError("unsupported origin class")


def _validate_origin(origin: str) -> str:
    if not isinstance(origin, str) or not _valid_origin(origin):
        raise ValueError("origin must use release:, pub:, or issuer:cik: prefix")
    return origin


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the append-only origin equivalence log."""
    with atomic_savepoint(conn, "origin_equivalences"):
        relations.ensure_schema(conn)
        conn.execute("""CREATE TABLE IF NOT EXISTS origin_inputs (
            record_key TEXT PRIMARY KEY REFERENCES dedup_candidate_records(record_id),
            provider TEXT NOT NULL,
            publisher TEXT,
            content_state TEXT,
            kind TEXT NOT NULL CHECK(kind IN ('body','headline','filing','fact')),
            cik TEXT,
            recorded_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS origin_inputs_no_update
            BEFORE UPDATE ON origin_inputs BEGIN
            SELECT RAISE(ABORT, 'origin inputs are append-only'); END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS origin_inputs_no_delete
            BEFORE DELETE ON origin_inputs BEGIN
            SELECT RAISE(ABORT, 'origin inputs are append-only'); END""")
        conn.execute("""CREATE TABLE IF NOT EXISTS origin_equivalences (
            old_origin TEXT NOT NULL,
            new_origin TEXT NOT NULL,
            discovered_at TEXT NOT NULL,
            relation_id TEXT NOT NULL,
            PRIMARY KEY(old_origin,new_origin,discovered_at,relation_id),
            CHECK(old_origin != new_origin))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_origin_equivalences_old_origin "
                     "ON origin_equivalences(old_origin)")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS origin_equivalences_no_update
            BEFORE UPDATE ON origin_equivalences BEGIN
            SELECT RAISE(ABORT, 'origin equivalences are append-only'); END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS origin_equivalences_no_delete
            BEFORE DELETE ON origin_equivalences BEGIN
            SELECT RAISE(ABORT, 'origin equivalences are append-only'); END""")


def record_origin_inputs(conn: sqlite3.Connection, record_key: str, *, provider: str,
                         publisher: str | None, content_state: str | None, kind: str,
                         cik: str | None, recorded_at: str) -> None:
    """Record immutable source metadata, accepting only exact replays."""
    record_key = _text(record_key, "record_key")
    provider = _text(provider, "provider")
    for value, field in ((publisher, "publisher"), (content_state, "content_state"), (cik, "cik")):
        if value is not None:
            _text(value, field)
    if kind not in {"body", "headline", "filing", "fact"}:
        raise ValueError("unsupported origin input kind")
    recorded_at = _time(recorded_at)
    expected = (provider, publisher, content_state, kind, cik, recorded_at)
    with atomic_savepoint(conn, "origin_equivalences"):
        ensure_schema(conn)
        if conn.execute("SELECT 1 FROM dedup_candidate_records WHERE record_id=?",
                        (record_key,)).fetchone() is None:
            raise ValueError("origin inputs require an existing candidate record")
        conn.execute("INSERT OR IGNORE INTO origin_inputs "
                     "(record_key,provider,publisher,content_state,kind,cik,recorded_at) "
                     "VALUES (?,?,?,?,?,?,?)", (record_key, *expected))
        prior = conn.execute("SELECT provider,publisher,content_state,kind,cik,recorded_at "
                             "FROM origin_inputs WHERE record_key=?", (record_key,)).fetchone()
        if prior != expected:
            raise ValueError("origin input replay conflicts with its immutable record")


def record_equivalence(conn: sqlite3.Connection, old_origin: str, new_origin: str, *,
                       discovered_at: str, relation_id: str) -> bool:
    """Append an origin merge and reject cycles in the complete equivalence log."""
    old_origin = _validate_origin(old_origin)
    new_origin = _validate_origin(new_origin)
    if old_origin == new_origin:
        raise ValueError("equivalent origins must differ")
    if _origin_class(old_origin) != _origin_class(new_origin):
        raise ValueError("equivalent origins must have the same priority class")
    if new_origin > old_origin:
        raise ValueError("new origin must be the lexicographically smaller canonical value")
    discovered_at = _time(discovered_at)
    relation_id = _text(relation_id, "relation_id")
    with atomic_savepoint(conn, "origin_equivalences"):
        ensure_schema(conn)
        relation = conn.execute("SELECT discovered_at FROM dedup_relations "
                                "WHERE relation_id=? AND status='active'",
                                (relation_id,)).fetchone()
        if relation is None:
            raise ValueError("origin equivalence requires an active relation")
        if discovered_at < relation[0]:
            raise ValueError("equivalence cannot precede its relation")
        successor = conn.execute("SELECT new_origin FROM origin_equivalences WHERE old_origin=? "
                                 "AND new_origin!=? LIMIT 1", (old_origin, new_origin)).fetchone()
        if successor is not None:
            raise ValueError("an origin may have only one successor")
        successors: dict[str, set[str]] = {}
        for left, right in conn.execute("SELECT old_origin,new_origin FROM origin_equivalences"):
            successors.setdefault(left, set()).add(right)
        todo, visited = [new_origin], set()
        while todo:
            current = todo.pop()
            if current == old_origin:
                raise ValueError("origin equivalence cycle")
            if current in visited:
                continue
            visited.add(current)
            todo.extend(successors.get(current, ()))
        before = conn.total_changes
        conn.execute("INSERT OR IGNORE INTO origin_equivalences VALUES (?,?,?,?)",
                     (old_origin, new_origin, discovered_at, relation_id))
        return conn.total_changes > before


def _require_tables(conn: sqlite3.Connection, *table_names: str) -> None:
    existing = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table_name in table_names:
        if table_name not in existing:
            raise LookupError(f"required table is missing: {table_name}")


def _components(conn: sqlite3.Connection, record_key: str, *, mode: str, at: str,
                snapshot_relation_ids: set[str] | None,
                rule_version: str | None = None) -> tuple[set[str], list[str]]:
    _require_tables(conn, "dedup_relations", *("duplicate_links",) if rule_version is None else ())
    members: set[str] = set()
    relation_ids: set[str] = set()
    frontier = {record_key}
    snapshot = snapshot_relation_ids or set()
    if mode == "published" and rule_version is not None:
        if any(relation_id.startswith("legacy:") for relation_id in snapshot):
            raise ValueError("versioned snapshots cannot contain legacy relation IDs")
        snapshot_ids: set[str] = set()
        for start in range(0, len(snapshot), 500):
            chunk = sorted(snapshot)[start:start + 500]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            snapshot_ids.update(row[0] for row in conn.execute(
                "SELECT DISTINCT relation_id FROM dedup_relations "
                f"WHERE rule_version=? AND relation_id IN ({placeholders})",
                (rule_version, *chunk),
            ))
        if snapshot_ids != snapshot:
            raise ValueError("published snapshot contains a missing or differently versioned relation")
    while frontier:
        next_frontier: set[str] = set()
        for start in range(0, len(frontier), 500):
            chunk = sorted(frontier)[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            params: list[object] = list(chunk)
            sql = ("SELECT a.relation_id,a.record_key_a,a.record_key_b FROM dedup_relations a "
                   "WHERE a.relation_type='same_release' AND a.status='active' "
                   f"AND (a.record_key_a IN ({placeholders}) OR a.record_key_b IN ({placeholders}))")
            params.extend(chunk)
            if rule_version is not None:
                sql += " AND a.rule_version=?"
                params.append(rule_version)
            if mode == "observed":
                sql += (" AND a.discovered_at<=? AND NOT EXISTS (SELECT 1 FROM dedup_relations r "
                        "WHERE r.relation_id=a.relation_id AND r.status='retracted' AND r.retracted_at<=?)")
                params.extend((at, at))
            elif snapshot:
                # Snapshot membership is applied in Python in bounded chunks below.
                pass
            else:
                sql += " AND 1=0"
            for relation_id, left, right in conn.execute(sql, params):
                if mode == "published" and relation_id not in snapshot:
                    continue
                relation_ids.add(relation_id)
                if left not in members:
                    next_frontier.add(left)
                if right not in members:
                    next_frontier.add(right)
        if mode == "published" and snapshot and rule_version is None:
            for start in range(0, len(frontier), 500):
                chunk = sorted(frontier)[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                params = [*chunk, *chunk]
                for left, right, relation_type, evidence_kind in conn.execute(
                    "SELECT record_a,record_b,relation_type,evidence_kind FROM duplicate_links "
                    f"WHERE record_a IN ({placeholders}) OR record_b IN ({placeholders})", params):
                    relation_id = relations.legacy_relation_id(left, right, relation_type, evidence_kind)
                    if relation_type == "same_release" and relation_id in snapshot:
                        relation_ids.add(relation_id)
                        if left not in members:
                            next_frontier.add(left)
                        if right not in members:
                            next_frontier.add(right)
        members.update(frontier)
        frontier = next_frontier - members
    return members, sorted(relation_ids)


def _apply_equivalences(conn: sqlite3.Connection, origin: str, at: str,
                        snapshot_relation_ids: set[str] | None, *, mode: str,
                        rule_version: str | None = None) -> str | None:
    _require_tables(conn, "origin_equivalences")
    seen: set[str] = set()
    while True:
        rows = conn.execute(
            "SELECT new_origin,discovered_at,relation_id FROM origin_equivalences "
            "WHERE old_origin=?", (origin,),
        ).fetchall()
        equivalences = [(new, relation_id) for new, discovered_at, relation_id in rows
                        if mode != "observed" or discovered_at <= at]
        if mode == "observed":
            visible_relation_ids = _visible_relation_ids(
                conn, at=at,
                relation_ids=[relation_id for _new, relation_id in equivalences],
                rule_version=rule_version,
            )
        else:
            visible_relation_ids = snapshot_relation_ids or set()
        successors = {new for new, relation_id in equivalences
                      if relation_id in visible_relation_ids}
        if not successors:
            return origin
        if len(successors) != 1 or origin in seen:
            return None
        seen.add(origin)
        origin = next(iter(successors))


def _visible_relation_ids(conn: sqlite3.Connection, *, at: str,
                          relation_ids: list[str],
                          rule_version: str | None = None) -> set[str]:
    visible: set[str] = set()
    relation_ids = sorted(set(relation_ids))
    for start in range(0, len(relation_ids), 500):
        chunk = relation_ids[start:start + 500]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        query = ("SELECT a.relation_id FROM dedup_relations a "
                 "WHERE a.status='active' "
                 f"AND a.relation_id IN ({placeholders})")
        params: tuple[object, ...] = tuple(chunk)
        if rule_version is not None:
            query += " AND a.rule_version=?"
            params += (rule_version,)
        query += (" AND a.discovered_at<=? AND NOT EXISTS (SELECT 1 FROM dedup_relations r "
                  "WHERE r.relation_id=a.relation_id AND r.status='retracted' "
                  "AND r.retracted_at<=?)")
        params += (at, at)
        visible.update(row[0] for row in conn.execute(query, params))
    return visible


def _release_origins(evidence_rows: list[dict]) -> tuple[set[str], set[str]]:
    releases, fingerprints = set(), set()
    for item in evidence_rows:
        if item.get("release_id_confirmed"):
            namespace = item["release_namespace"].strip().casefold()
            if ":" in namespace:
                # release:<namespace>:<id> must split one way; the id may hold colons, the namespace may not.
                raise ValueError("release namespace cannot contain ':'")
            releases.add(f"release:{namespace}:{item['release_id'].strip()}")
        if item.get("body_fingerprint_confirmed"):
            fingerprints.add(f"release:fp:{item['fingerprint_version']}:{item['body_fingerprint'][:16]}")
    return releases, fingerprints


def _publisher_origins(origin_rows: list[tuple], registry: tuple[PublisherEntry, ...]) -> set[str]:
    origins = set()
    for provider, publisher, content_state, _kind, _cik in origin_rows:
        if provider is None or publisher is None:
            continue
        matches = [entry for entry in registry if entry.provider == provider
                   and entry.publisher == publisher and entry.content_state == content_state]
        if len(matches) > 1:
            raise ValueError("publisher registry contains duplicate provider/publisher entries")
        for entry in matches:
            if entry.evidence_ref == "PENDING_SAMPLE":
                raise ValueError("PENDING_SAMPLE registry evidence cannot establish an origin")
            origins.add("pub:" + entry.family)
    return origins


def _effective_time(value: str, field: str) -> str:
    normalized = _time(value)
    if normalized != value or not value.endswith("Z"):
        raise ValueError(f"{field} must be a UTC timestamp in YYYY-MM-DDTHH:MM:SSZ form")
    return normalized


def _validate_registry(registry: tuple[PublisherEntry, ...]) -> None:
    intervals: dict[tuple[str, str, str], list[tuple[str, str | None]]] = {}
    for entry in registry:
        effective_from = _effective_time(entry.effective_from, "effective_from")
        effective_until = (None if entry.effective_until is None else
                           _effective_time(entry.effective_until, "effective_until"))
        if effective_until is not None and effective_until <= effective_from:
            raise ValueError("effective_until must be later than effective_from")
        key = (entry.provider, entry.publisher, entry.content_state)
        for prior_from, prior_until in intervals.setdefault(key, []):
            if (prior_until is None or effective_from < prior_until) and (
                effective_until is None or prior_from < effective_until
            ):
                raise ValueError("publisher registry has overlapping duplicate entries")
        intervals[key].append((effective_from, effective_until))


def _applicable_registry(registry: tuple[PublisherEntry, ...], as_of: str
                         ) -> tuple[PublisherEntry, ...]:
    _validate_registry(registry)
    as_of = _effective_time(as_of, "registry_as_of")
    return tuple(
        entry for entry in registry
        if entry.effective_from <= as_of
        and (entry.effective_until is None or as_of < entry.effective_until)
    )


def _issuer_origins(origin_rows: list[tuple]) -> set[str]:
    origins = set()
    for provider, _publisher, _content_state, kind, cik in origin_rows:
        if kind != "filing" or provider not in SEC_PROVIDERS:
            continue
        if cik is None:
            continue
        if not isinstance(cik, str) or not re.fullmatch(r"[0-9]{1,10}", cik) or int(cik) == 0:
            raise ValueError("SEC filing CIK must be a present, nonzero numeric value")
        origins.add("issuer:cik:" + cik.zfill(10))
    return origins


def _derived_record_keys(origin_rows: dict[str, tuple], at: str) -> set[str]:
    """Return records whose registered URL role is attribution at ``at``."""
    from catalyst_data.dedup import url_identity_registry

    url_identity_registry.validate_registry()
    derived = set()
    for key, row in origin_rows.items():
        provider, publisher = row[0], row[1]
        if not isinstance(provider, str) or not isinstance(publisher, str):
            continue
        matches = [entry for entry in url_identity_registry.URL_IDENTITY_REGISTRY
                   if entry.provider.strip().casefold() == provider.strip().casefold()
                   and entry.publisher.strip().casefold() == publisher.strip().casefold()
                   and entry.effective_from <= at
                   and (entry.effective_until is None or at < entry.effective_until)]
        if len(matches) > 1:
            raise ValueError("URL identity registry has ambiguous entries for one key")
        if matches and matches[0].url_role == "attribution":
            derived.add(key)
    return derived


def _resolve_priority(conn: sqlite3.Connection, origins: set[str], basis: str, at: str,
                      relation_ids: list[str], *, mode: str,
                      snapshot_relation_ids: set[str] | None,
                      registry_hash: str, rule_version: str | None = None) -> dict | None:
    canonical = {_apply_equivalences(conn, origin, at, snapshot_relation_ids, mode=mode,
                                     rule_version=rule_version)
                 for origin in origins}
    if None in canonical or len(canonical) > 1:
        return {"origin": None, "origin_status": "conflict", "origin_basis": basis,
                "relation_ids": relation_ids, "registry_sha256": registry_hash}
    if canonical:
        origin = _validate_origin(next(iter(canonical)))
        return {"origin": origin, "origin_status": "resolved", "origin_basis": basis,
                "relation_ids": relation_ids, "registry_sha256": registry_hash}
    return None


def assign_origin(conn: sqlite3.Connection, record_key: str, *, at: str, mode: str,
                  registry=PUBLISHER_REGISTRY, snapshot_relation_ids=None,
                  registry_as_of: str | None = None,
                  rule_version: str | None = None) -> dict:
    """Resolve one record's origin without combining evidence across priorities."""
    record_key = _text(record_key, "record_key")
    at = _time(at)
    if rule_version == DERIVATIVE_RULE_VERSION:
        from catalyst_data.dedup import rules_v3
        rules_v3.check_pins()
    if mode not in {"observed", "published"}:
        raise ValueError("mode must be observed or published")
    if not isinstance(registry, tuple):
        registry = tuple(registry)
    if rule_version == DERIVATIVE_RULE_VERSION and registry != PUBLISHER_REGISTRY:
        raise ValueError("d1-rules-v3 requires the pinned publisher registry")
    for entry in registry:
        if not isinstance(entry, PublisherEntry):
            raise ValueError("registry entries must be PublisherEntry values")
        _text(entry.provider, "provider")
        _text(entry.publisher, "publisher")
        _text(entry.content_state, "content_state")
        _text(entry.family, "family")
        _text(entry.evidence_ref, "evidence_ref")
        if not re.fullmatch(r"[a-z0-9_-]+", entry.family):
            raise ValueError("publisher family must be a normalized identifier")
    if mode == "published" and snapshot_relation_ids is None:
        raise ValueError("published mode requires snapshot_relation_ids")
    if mode == "observed":
        registry_as_of_value = at
    else:
        if registry_as_of is None:
            raise ValueError("published mode requires registry_as_of")
        registry_as_of_value = registry_as_of
    applicable_registry = _applicable_registry(registry, registry_as_of_value)
    registry_hash = registry_sha256(applicable_registry)
    relation_snapshot = None if snapshot_relation_ids is None else set(snapshot_relation_ids)
    if mode == "published" and rule_version is not None:
        if any(relation_id.startswith("legacy:") for relation_id in relation_snapshot):
            raise ValueError("versioned snapshots cannot contain legacy relation IDs")
        # _components validates that every snapshot ID exists under this version.
    _require_tables(conn, "dedup_candidate_records", "origin_inputs", "origin_equivalences")
    members, relation_ids = _components(conn, record_key, mode=mode, at=at,
                                        snapshot_relation_ids=relation_snapshot,
                                        rule_version=rule_version)
    member_keys = sorted(members)
    evidence_rows = []
    for start in range(0, len(member_keys), 500):
        chunk = member_keys[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        evidence_rows.extend(
            (key, json.loads(payload)) for key, payload in conn.execute(
                f"SELECT record_id,evidence_json FROM dedup_candidate_records "
                f"WHERE record_id IN ({placeholders}) ORDER BY record_id", chunk
            )
        )
    if record_key not in {key for key, _item in evidence_rows}:
        raise ValueError("record_key is not present in the candidate record index")
    input_rows_by_key: dict[str, tuple] = {}
    observed_members: set[str] = set()
    for start in range(0, len(member_keys), 500):
        chunk = member_keys[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        if mode == "observed":
            rows = conn.execute(
                f"SELECT record_key,provider,publisher,content_state,kind,cik FROM origin_inputs "
                f"WHERE record_key IN ({placeholders}) AND recorded_at<=? ORDER BY record_key",
                (*chunk, at),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT record_key,provider,publisher,content_state,kind,cik FROM origin_inputs "
                f"WHERE record_key IN ({placeholders}) ORDER BY record_key", chunk
            ).fetchall()
        observed_members.update(row[0] for row in rows)
        input_rows_by_key.update((row[0], row[1:]) for row in rows)
    derived_keys = (_derived_record_keys(input_rows_by_key, at)
                    if rule_version == DERIVATIVE_RULE_VERSION else set())
    evidence_values = [item for key, item in evidence_rows
                       if key in observed_members and key not in derived_keys]
    input_rows = [row for key, row in input_rows_by_key.items() if key not in derived_keys]
    release_values, fingerprint_values = _release_origins(evidence_values)
    issuer_values = _issuer_origins(input_rows)
    if release_values:
        result = _resolve_priority(conn, release_values, "confirmed_release", at, relation_ids,
                                   mode=mode, snapshot_relation_ids=relation_snapshot,
                                   registry_hash=registry_hash, rule_version=rule_version)
    elif fingerprint_values:
        result = _resolve_priority(conn, fingerprint_values, "full_body_fingerprint", at, relation_ids,
                                   mode=mode, snapshot_relation_ids=relation_snapshot,
                                   registry_hash=registry_hash, rule_version=rule_version)
    else:
        result = None
    if result is not None:
        return result
    result = _resolve_priority(conn, _publisher_origins(input_rows, applicable_registry),
                               "confirmed_publisher", at, relation_ids,
                               mode=mode, snapshot_relation_ids=relation_snapshot,
                               registry_hash=registry_hash, rule_version=rule_version)
    if result is not None:
        return result
    result = _resolve_priority(conn, issuer_values, "issuer_cik", at, relation_ids,
                               mode=mode, snapshot_relation_ids=relation_snapshot,
                               registry_hash=registry_hash, rule_version=rule_version)
    if result is not None:
        return result
    return {"origin": None, "origin_status": "unresolved", "origin_basis": "unresolved",
            "relation_ids": relation_ids, "registry_sha256": registry_hash}


def link_same_release(conn: sqlite3.Connection, record_key_a: str, record_key_b: str, *,
                      rule_id: str, rule_version: str, basis: str, discovered_at: str,
                      registry=PUBLISHER_REGISTRY) -> dict[str, object]:
    """Record a same-release relation and merge distinct resolved same-class origins."""
    left = assign_origin(conn, record_key_a, at=discovered_at, mode="observed", registry=registry)
    right = assign_origin(conn, record_key_b, at=discovered_at, mode="observed", registry=registry)
    equivalence = None
    with atomic_savepoint(conn, "origin_equivalences"):
        relation_id = relations.record_relation(
            conn, record_key_a, record_key_b, relation_type="same_release", rule_id=rule_id,
            rule_version=rule_version, basis=basis, discovered_at=discovered_at,
        )
        if left["origin_status"] == right["origin_status"] == "resolved":
            origin_a, origin_b = left["origin"], right["origin"]
            if (_origin_class(origin_a) == _origin_class(origin_b) and origin_a != origin_b):
                old_origin, new_origin = sorted((origin_a, origin_b), reverse=True)
                record_equivalence(conn, old_origin, new_origin, discovered_at=discovered_at,
                                   relation_id=relation_id)
                equivalence = (old_origin, new_origin)
    return {"relation_id": relation_id, "equivalence": equivalence}
