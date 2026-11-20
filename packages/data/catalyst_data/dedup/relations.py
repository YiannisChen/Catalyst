"""Append-only, time-bounded relations between ingested record versions."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable

from catalyst_data.dedup import ingestion, links
from catalyst_data.ingestion.time import timefields
from catalyst_data.storage.atomic import atomic_savepoint


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a nonempty trimmed value")
    return value


def _time(value: str, field: str) -> str:
    _text(value, field)
    try:
        return timefields.stamp(timefields.stored(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a timezone-aware timestamp") from exc


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the relation event ledger without changing legacy link tables."""
    with atomic_savepoint(conn, "dedup_relations"):
        ingestion.ensure_schema(conn)
        conn.execute("""CREATE TABLE IF NOT EXISTS dedup_relations (
            relation_id TEXT NOT NULL,
            record_key_a TEXT NOT NULL,
            record_key_b TEXT NOT NULL,
            relation_type TEXT NOT NULL CHECK(relation_type IN ('same_article','same_release')),
            rule_id TEXT NOT NULL,
            rule_version TEXT NOT NULL,
            basis TEXT NOT NULL,
            discovered_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('candidate','active','retracted')),
            retracted_at TEXT,
            retraction_reason TEXT,
            PRIMARY KEY(relation_id, status),
            CHECK(record_key_a < record_key_b),
            CHECK((status='retracted' AND retracted_at IS NOT NULL AND retraction_reason IS NOT NULL)
               OR (status!='retracted' AND retracted_at IS NULL AND retraction_reason IS NULL))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dedup_relations_members "
                     "ON dedup_relations(record_key_a, record_key_b, discovered_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dedup_relations_status_discovered "
                     "ON dedup_relations(status, discovered_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dedup_relations_record_key_b "
                     "ON dedup_relations(record_key_b)")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS dedup_relations_no_update
            BEFORE UPDATE ON dedup_relations BEGIN
            SELECT RAISE(ABORT, 'dedup relations are append-only'); END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS dedup_relations_no_delete
            BEFORE DELETE ON dedup_relations BEGIN
            SELECT RAISE(ABORT, 'dedup relations are append-only'); END""")


def record_relation(
    conn: sqlite3.Connection,
    record_key_a: str,
    record_key_b: str,
    *,
    relation_type: str,
    rule_id: str,
    rule_version: str,
    basis: str,
    discovered_at: str,
    status: str = "active",
    retracted_at: str | None = None,
    retraction_reason: str | None = None,
) -> str:
    """Append a deterministic relation event; exact replays keep one row."""
    record_key_a = _text(record_key_a, "record_key_a")
    record_key_b = _text(record_key_b, "record_key_b")
    if record_key_a == record_key_b:
        raise ValueError("a relation requires two distinct record keys")
    record_key_a, record_key_b = sorted((record_key_a, record_key_b))
    if relation_type not in {"same_article", "same_release"}:
        raise ValueError("unsupported relation_type")
    if status not in {"candidate", "active", "retracted"}:
        raise ValueError("unsupported relation status")
    rule_id = _text(rule_id, "rule_id")
    rule_version = _text(rule_version, "rule_version")
    basis = _text(basis, "basis")
    discovered_at = _time(discovered_at, "discovered_at")
    if status == "retracted":
        if retracted_at is None or retraction_reason is None:
            raise ValueError("retraction requires retracted_at and retraction_reason")
        retracted_at = _time(retracted_at, "retracted_at")
        retraction_reason = _text(retraction_reason, "retraction_reason")
    elif retracted_at is not None or retraction_reason is not None:
        raise ValueError("only retracted relations may include retraction fields")
    identity = [record_key_a, record_key_b, relation_type, rule_id, rule_version, basis]
    relation_id = hashlib.sha256(json.dumps(identity, separators=(",", ":"),
                                  ensure_ascii=False).encode("utf-8")).hexdigest()
    with atomic_savepoint(conn, "dedup_relations"):
        ensure_schema(conn)
        active = conn.execute(
            "SELECT discovered_at FROM dedup_relations WHERE relation_id=? AND status='active'",
            (relation_id,),
        ).fetchone()
        if status == "retracted" and active is None:
            raise ValueError("only an existing active relation can be retracted")
        if status == "retracted" and active is not None and retracted_at < active[0]:
            raise ValueError("retracted_at cannot precede active relation discovery")
        conn.execute("""INSERT OR IGNORE INTO dedup_relations
            (relation_id,record_key_a,record_key_b,relation_type,rule_id,rule_version,basis,
             discovered_at,status,retracted_at,retraction_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (relation_id, record_key_a, record_key_b, relation_type, rule_id, rule_version,
             basis, discovered_at, status, retracted_at, retraction_reason))
        existing = conn.execute("SELECT discovered_at,retracted_at,retraction_reason FROM dedup_relations "
                                "WHERE relation_id=? AND status=?", (relation_id, status)).fetchone()
        expected = (discovered_at, retracted_at, retraction_reason)
        if existing != expected:
            raise ValueError("relation replay conflicts with its immutable event")
    return relation_id


def visible_relations(conn: sqlite3.Connection, *, mode: str, at: str,
                      snapshot_relation_ids=None,
                      rule_version: str | None = None) -> list[dict[str, str | None]]:
    """Return active relations visible under observed time or a pinned snapshot."""
    if mode not in {"observed", "published"}:
        raise ValueError("mode must be observed or published")
    at = _time(at, "at")
    _require_tables(conn, "dedup_relations", *("duplicate_links",) if rule_version is None else ())
    snapshot = None if snapshot_relation_ids is None else set(snapshot_relation_ids)
    if mode == "published" and snapshot is None:
        raise ValueError("published mode requires snapshot_relation_ids")
    if mode == "published" and rule_version is not None:
        if any(relation_id.startswith("legacy:") for relation_id in snapshot):
            raise ValueError("versioned snapshots cannot contain legacy relation IDs")
        snapshot_ids = set()
        if snapshot:
            for start in range(0, len(snapshot), 500):
                chunk = sorted(snapshot)[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                snapshot_ids.update(row[0] for row in conn.execute(
                    "SELECT DISTINCT relation_id FROM dedup_relations "
                    f"WHERE rule_version=? AND relation_id IN ({placeholders})",
                    (rule_version, *chunk),
                ))
        if snapshot_ids != snapshot:
            raise ValueError("published snapshot contains a missing or differently versioned relation")
    query = ("SELECT relation_id,record_key_a,record_key_b,relation_type,rule_id,rule_version,"
             "basis,discovered_at,status,retracted_at FROM dedup_relations")
    params = ()
    if rule_version is not None:
        query += " WHERE rule_version=?"
        params = (rule_version,)
    if mode == "observed":
        query += (" AND " if rule_version is not None else " WHERE ")
        # Parenthesised so a rule_version filter covers both the active and the retraction arm.
        query += "((status='active' AND discovered_at<=?) OR (status='retracted' AND retracted_at<=?))"
        rows = conn.execute(query + " ORDER BY relation_id,status", (*params, at, at)).fetchall()
    else:
        rows = conn.execute(query + " ORDER BY relation_id,status", params).fetchall()
    by_id: dict[str, list[tuple]] = {}
    for row in rows:
        by_id.setdefault(row[0], []).append(row)
    visible: list[dict[str, str | None]] = []
    for relation_id, events in by_id.items():
        original = next((row for row in events if row[8] != "retracted"), None)
        if original is None or original[8] != "active":
            continue
        if mode == "observed":
            if original[7] > at:
                continue
            if any(row[8] == "retracted" and row[9] <= at for row in events):
                continue
        elif relation_id not in snapshot:
            continue
        visible.append(dict(zip(("relation_id", "record_key_a", "record_key_b", "relation_type",
                                 "rule_id", "rule_version", "basis", "discovered_at", "status"),
                                original[:9])))
    if mode == "published":
        if rule_version is None and any(relation_id.startswith("legacy:") for relation_id in snapshot):
            for record_a, record_b, relation_type, evidence_kind in conn.execute(
                "SELECT record_a,record_b,relation_type,evidence_kind FROM duplicate_links "
                "ORDER BY record_a,record_b,relation_type,evidence_kind"
            ):
                relation_id = legacy_relation_id(record_a, record_b, relation_type, evidence_kind)
                if relation_id in snapshot:
                    visible.append({"relation_id": relation_id, "record_key_a": record_a,
                                    "record_key_b": record_b, "relation_type": relation_type,
                                    "rule_id": "legacy_duplicate_links", "rule_version": "legacy-v1",
                                    "basis": evidence_kind, "discovered_at": None, "status": "active"})
    return sorted(visible, key=lambda row: row["relation_id"])


def _require_tables(conn: sqlite3.Connection, *table_names: str) -> None:
    existing = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table_name in table_names:
        if table_name not in existing:
            raise LookupError(f"required table is missing: {table_name}")


def relation_set_sha256(relation_ids) -> str:
    """Hash a canonical unique set of relation IDs."""
    ids = [_text(item, "relation_id") for item in relation_ids]
    if len(ids) != len(set(ids)):
        ids = sorted(set(ids))
    payload = json.dumps(sorted(ids), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def legacy_relation_id(record_a: str, record_b: str, relation_type: str,
                       evidence_kind: str) -> str:
    """Return the snapshot key for one unchanged legacy duplicate_links row."""
    values = [_text(record_a, "record_a"), _text(record_b, "record_b"),
              _text(relation_type, "relation_type"), _text(evidence_kind, "evidence_kind")]
    if values[0] >= values[1]:
        raise ValueError("legacy record IDs must be in ascending order")
    if relation_type not in {"same_article", "same_release"}:
        raise ValueError("unsupported legacy relation_type")
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return "legacy:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def rerun_legacy_relations(conn: sqlite3.Connection, *, rule_version: str,
                           discovered_at: str) -> int:
    """Re-derive deterministic relations from records, assigning the rerun instant."""
    rule_version = _text(rule_version, "rule_version")
    discovered_at = _time(discovered_at, "discovered_at")
    with atomic_savepoint(conn, "dedup_relations"):
        ensure_schema(conn)
        before = conn.execute("SELECT COUNT(*) FROM dedup_relations").fetchone()[0]
        pair_rows = conn.execute(
            "SELECT k1.record_id,k2.record_id,r1.evidence_json,r1.provider_record_id_kind,"
            "r2.evidence_json,r2.provider_record_id_kind "
            "FROM dedup_candidate_keys AS k1 "
            "JOIN dedup_candidate_keys AS k2 "
            "ON k1.key_kind=k2.key_kind AND k1.key_namespace=k2.key_namespace "
            "AND k1.key_value=k2.key_value AND k1.record_id<k2.record_id "
            "JOIN dedup_candidate_records AS r1 ON r1.record_id=k1.record_id "
            "JOIN dedup_candidate_records AS r2 ON r2.record_id=k2.record_id "
            "WHERE k1.key_kind!='provider_record_id' OR "
            "(r1.provider_record_id_kind='article' AND r2.provider_record_id_kind='article') "
            "ORDER BY k1.record_id,k2.record_id"
        )
        seen_pairs: set[tuple[str, str]] = set()
        for key_a, key_b, payload_a, kind_a, payload_b, kind_b in pair_rows:
            pair = (key_a, key_b)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            record_a = ingestion._record_for_classification(
                links.EvidenceRecord(**json.loads(payload_a)), kind_a, key_a)
            record_b = ingestion._record_for_classification(
                links.EvidenceRecord(**json.loads(payload_b)), kind_b, key_b)
            for relation_type, basis in sorted(links.classify_pair(record_a, record_b)):
                value = _legacy_basis_value(record_a, record_b, basis)
                _insert_rerun_relation(conn, key_a, key_b, relation_type, basis,
                                       rule_version, value, discovered_at)
        return conn.execute("SELECT COUNT(*) FROM dedup_relations").fetchone()[0] - before


def rerun_classified_relations(
    conn: sqlite3.Connection,
    *,
    discovered_at: str,
    rule_version: str,
    classify_pair: Callable[..., set[links.Link]],
    basis_value: Callable[[links.EvidenceRecord, links.EvidenceRecord, str], str],
    prepare: Callable[[sqlite3.Connection], None],
) -> dict:
    """Re-derive versioned relations using a rule-set's classifier and basis policy."""
    discovered_at = _time(discovered_at, "discovered_at")
    with atomic_savepoint(conn, "dedup_relations"):
        ensure_schema(conn)
        prepare(conn)
        pair_rows = conn.execute(
            "SELECT k1.record_id,k2.record_id,r1.evidence_json,r1.provider_record_id_kind,"
            "r2.evidence_json,r2.provider_record_id_kind "
            "FROM dedup_candidate_keys AS k1 "
            "JOIN dedup_candidate_keys AS k2 "
            "ON k1.key_kind=k2.key_kind AND k1.key_namespace=k2.key_namespace "
            "AND k1.key_value=k2.key_value AND k1.record_id<k2.record_id "
            "JOIN dedup_candidate_records AS r1 ON r1.record_id=k1.record_id "
            "JOIN dedup_candidate_records AS r2 ON r2.record_id=k2.record_id "
            "WHERE k1.key_kind!='provider_record_id' OR "
            "(r1.provider_record_id_kind='article' AND r2.provider_record_id_kind='article') "
            "ORDER BY k1.record_id,k2.record_id"
        ).fetchall()
        input_rows = {row[0]: tuple(row[1:5]) for row in conn.execute(
            "SELECT record_key,provider,publisher,content_state,kind FROM origin_inputs"
        )}
        counts: dict[str, int] = {}
        gap_pairs: set[tuple[str, str]] = set()
        seen_pairs: set[tuple[str, str]] = set()
        for key_a, key_b, payload_a, kind_a, payload_b, kind_b in pair_rows:
            pair = (key_a, key_b)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            record_a = ingestion._record_for_classification(
                links.EvidenceRecord(**json.loads(payload_a)), kind_a, key_a
            )
            record_b = ingestion._record_for_classification(
                links.EvidenceRecord(**json.loads(payload_b)), kind_b, key_b
            )
            left_inputs = input_rows.get(key_a)
            right_inputs = input_rows.get(key_b)
            left_url = links.normalize_identity_url(record_a.url)
            if left_url is not None and left_url == links.normalize_identity_url(record_b.url) and (
                left_inputs is None or right_inputs is None
                or left_inputs[1] is None or not left_inputs[1].strip()
                or right_inputs[1] is None or not right_inputs[1].strip()
            ):
                gap_pairs.add(pair)
            for relation_type, rule_id in sorted(
                classify_pair(record_a, record_b, left_inputs, right_inputs)
            ):
                basis = basis_value(record_a, record_b, rule_id)
                key_left, key_right = sorted((key_a, key_b))
                identity = [key_left, key_right, relation_type, rule_id, rule_version, basis]
                relation_id = hashlib.sha256(json.dumps(
                    identity, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")).hexdigest()
                conn.execute(
                    "INSERT OR IGNORE INTO dedup_relations "
                    "(relation_id,record_key_a,record_key_b,relation_type,rule_id,rule_version,basis,"
                    "discovered_at,status,retracted_at,retraction_reason) "
                    "VALUES (?,?,?,?,?,?,?,?,'active',NULL,NULL)",
                    (relation_id, key_left, key_right, relation_type, rule_id, rule_version,
                     basis, discovered_at),
                )
                counts[rule_id] = counts.get(rule_id, 0) + 1
        undecidable = sorted(
            key for (key,) in conn.execute("SELECT record_id FROM dedup_candidate_records")
            if key not in input_rows or input_rows[key][1] is None
            or not str(input_rows[key][1]).strip()
        )
    return {
        "counts_by_rule": dict(sorted(counts.items())),
        "canonical_url_gaps": [list(pair) for pair in sorted(gap_pairs)],
        "publisher_missing_record_keys": undecidable,
    }


def _legacy_basis_value(left: links.EvidenceRecord, right: links.EvidenceRecord,
                        basis: str) -> str:
    if basis == "canonical_url":
        value = links.normalize_identity_url(left.url)
        if value is None or value != links.normalize_identity_url(right.url):
            raise RuntimeError("classified canonical URL pair has inconsistent identity")
        return value
    if basis == "confirmed_release_id":
        return f"{left.release_namespace.strip().casefold()}:{left.release_id.strip()}"
    if basis == "full_body_fingerprint":
        return f"{left.fingerprint_version}:{left.body_fingerprint[:16]}"
    if basis == "provider_record_id":
        return f"{left.provider.strip().casefold()}:{left.provider_record_id.strip()}"
    raise ValueError(f"unsupported legacy relation basis: {basis}")


def _insert_rerun_relation(conn: sqlite3.Connection, key_a: str, key_b: str,
                           relation_type: str, rule_id: str, rule_version: str,
                           basis: str, discovered_at: str) -> None:
    key_a, key_b = sorted((key_a, key_b))
    identity = [key_a, key_b, relation_type, rule_id, rule_version, basis]
    relation_id = hashlib.sha256(json.dumps(identity, separators=(",", ":"),
                                  ensure_ascii=False).encode("utf-8")).hexdigest()
    conn.execute("INSERT OR IGNORE INTO dedup_relations "
                 "(relation_id,record_key_a,record_key_b,relation_type,rule_id,rule_version,"
                 "basis,discovered_at,status,retracted_at,retraction_reason) "
                 "VALUES (?,?,?,?,?,?,?,?, 'active',NULL,NULL)",
                 (relation_id, key_a, key_b, relation_type, rule_id, rule_version,
                  basis, discovered_at))
    row = conn.execute("SELECT discovered_at,retracted_at,retraction_reason FROM dedup_relations "
                       "WHERE relation_id=? AND status='active'", (relation_id,)).fetchone()
    if row != (discovered_at, None, None):
        raise ValueError("relation rerun conflicts with its immutable event")
