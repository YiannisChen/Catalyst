"""Verified origin assertions and auditable same-release supersessions.

Raw provider names, URLs and accession strings never establish an origin here.
Callers must supply an explicit assertion and an evidence reference.
"""
from __future__ import annotations

import re

from catalyst_data.dedup import links
from catalyst_data.storage.atomic import atomic_savepoint


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f'{field} must be a nonempty trimmed identifier')
    return value


def ensure_schema(conn):
    with atomic_savepoint(conn, "verified_origins"):
        links.ensure_schema(conn)
        conn.execute('''CREATE TABLE IF NOT EXISTS origin_supersessions (
            old_assertion_id TEXT PRIMARY KEY REFERENCES origin_assertions(assertion_id),
            new_assertion_id TEXT NOT NULL REFERENCES origin_assertions(assertion_id),
            evidence_ref TEXT NOT NULL,
            CHECK(old_assertion_id != new_assertion_id))''')
        conn.execute('''CREATE TRIGGER IF NOT EXISTS origin_supersessions_no_update
            BEFORE UPDATE ON origin_supersessions BEGIN
            SELECT RAISE(ABORT, 'origin supersessions are append-only'); END''')
        conn.execute('''CREATE TRIGGER IF NOT EXISTS origin_supersessions_no_delete
            BEFORE DELETE ON origin_supersessions BEGIN
            SELECT RAISE(ABORT, 'origin supersessions are append-only'); END''')
        conn.execute('''CREATE TRIGGER IF NOT EXISTS origin_supersessions_validate
            BEFORE INSERT ON origin_supersessions BEGIN
            SELECT CASE WHEN NEW.evidence_ref IS NULL OR length(trim(NEW.evidence_ref))=0
                THEN RAISE(ABORT, 'supersession requires an evidence reference') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM origin_assertions old JOIN origin_assertions new
                ON old.record_id=new.record_id
                WHERE old.assertion_id=NEW.old_assertion_id
                  AND new.assertion_id=NEW.new_assertion_id
            ) THEN RAISE(ABORT, 'supersession requires existing assertions of one record') END;
            SELECT CASE WHEN EXISTS (
                WITH RECURSIVE successors(assertion_id) AS (
                    SELECT NEW.new_assertion_id
                    UNION
                    SELECT s.new_assertion_id FROM origin_supersessions s
                    JOIN successors r ON s.old_assertion_id=r.assertion_id
                ) SELECT 1 FROM successors WHERE assertion_id=NEW.old_assertion_id
            ) THEN RAISE(ABORT, 'origin supersession cycle') END;
            END''')
        conn.execute('CREATE INDEX IF NOT EXISTS duplicate_links_release_b ON duplicate_links(relation_type,record_b)')


def record_verified_origin(conn, record_id, *, origin_id, method, evidence_ref):
    record_id = _text(record_id, 'record_id')
    origin_id = _text(origin_id, 'origin_id')
    evidence_ref = _text(evidence_ref, 'evidence_ref')
    if method not in {'confirmed_publisher', 'confirmed_release'}:
        raise ValueError('unsupported verified origin method')
    if origin_id.startswith('filing:'):
        raise ValueError('accession is not an origin')
    with atomic_savepoint(conn, "verified_origins"):
        ensure_schema(conn)
        return links.append_origin_assertion(conn, record_id, origin_id=origin_id,
                                             method=method, evidence_ref=evidence_ref)


def record_issuer(conn, record_id, *, cik, identity_source, evidence_ref):
    record_id = _text(record_id, 'record_id')
    evidence_ref = _text(evidence_ref, 'evidence_ref')
    if identity_source not in {'filing', 'accession_registry'}:
        raise ValueError('issuer identity source must be filing or fixed accession registry')
    if isinstance(cik, bool) or not isinstance(cik, (str, int)):
        raise ValueError('CIK must be a valid issuer identifier')
    raw = str(cik)
    if not re.fullmatch(r'[0-9]{1,10}', raw) or int(raw) == 0:
        raise ValueError('CIK must be a valid issuer identifier')
    with atomic_savepoint(conn, "verified_origins"):
        ensure_schema(conn)
        return links.append_origin_assertion(conn, record_id, origin_id='issuer:cik:' + raw.zfill(10),
                                             method='issuer_cik:' + identity_source, evidence_ref=evidence_ref)


def _active(conn, record_id):
    transitions = conn.execute('''SELECT s.old_assertion_id,s.new_assertion_id,s.evidence_ref,n.record_id
        FROM origin_supersessions s JOIN origin_assertions old ON old.assertion_id=s.old_assertion_id
        LEFT JOIN origin_assertions n ON n.assertion_id=s.new_assertion_id
        WHERE old.record_id=?''', (record_id,)).fetchall()
    graph = {}
    for old_id, new_id, evidence_ref, new_record in transitions:
        if new_record != record_id or not isinstance(evidence_ref, str) or not evidence_ref.strip():
            raise ValueError('origin supersession is invalid')
        graph[old_id] = new_id
    for start in graph:
        seen = set()
        current = start
        while current in graph:
            if current in seen:
                raise ValueError('origin supersession cycle')
            seen.add(current)
            current = graph[current]
    rows = conn.execute('''SELECT a.assertion_id,a.origin_id,a.method,a.evidence_ref
        FROM origin_assertions a LEFT JOIN origin_supersessions s ON s.old_assertion_id=a.assertion_id
        WHERE a.record_id=? AND s.old_assertion_id IS NULL''', (record_id,)).fetchall()
    return [row for row in rows if row[1] and isinstance(row[3], str) and row[3].strip() and (
        (row[2] in {'issuer_cik:filing', 'issuer_cik:accession_registry'}
         and re.fullmatch(r'issuer:cik:[0-9]{10}', row[1]))
        or (row[2] in {'confirmed_publisher', 'confirmed_release'} and not row[1].startswith('filing:')))]


def resolve_origins(conn, record_ids):
    ids = tuple(record_ids)
    if any(not isinstance(item, str) or not item.strip() for item in ids) or len(ids) != len(set(ids)):
        raise ValueError('record_ids must be unique nonempty identifiers')
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute('BEGIN')
    try:
        required = {'origin_assertions', 'origin_supersessions'}
        present = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)", tuple(sorted(required))
        )}
        if present != required:
            raise ValueError('verified origin catalog schema is missing')
        origins, unresolved, conflicts, records = set(), [], [], {}
        for record_id in sorted(ids):
            values = {row[1] for row in _active(conn, record_id)}
            if len(values) == 1:
                origins.update(values)
                records[record_id] = {'status': 'resolved', 'origin_id': next(iter(values))}
            elif not values:
                unresolved.append(record_id)
                records[record_id] = {'status': 'unresolved', 'origin_id': None}
            else:
                conflicts.append(record_id)
                records[record_id] = {'status': 'conflict', 'origin_id': None}
        return {'origin_ids': sorted(origins), 'independent_origin_count': len(origins),
                'unresolved_record_ids': unresolved, 'conflict_record_ids': conflicts,
                'records': records}
    finally:
        if owns_transaction:
            conn.rollback()


def supersede_release_group(conn, record_id, *, release_origin, evidence_ref,
                            rule_version=None):
    record_id = _text(record_id, 'record_id')
    release_origin = _text(release_origin, 'release_origin')
    evidence_ref = _text(evidence_ref, 'evidence_ref')
    if not release_origin.startswith('release:'):
        raise ValueError('release origin must identify the confirmed release')
    with atomic_savepoint(conn, "verified_origins"):
        ensure_schema(conn)
        visited, todo = set(), [record_id]
        while todo:
            current = todo.pop()
            if current in visited:
                continue
            visited.add(current)
            if rule_version is None:
                rows = conn.execute('''SELECT record_a,record_b FROM duplicate_links
                    WHERE relation_type='same_release' AND (record_a=? OR record_b=?)''',
                    (current, current)).fetchall()
            else:
                rows = conn.execute('''SELECT a.record_key_a,a.record_key_b FROM dedup_relations a
                    WHERE a.relation_type='same_release' AND a.status='active'
                      AND a.rule_version=? AND (a.record_key_a=? OR a.record_key_b=?)
                      AND NOT EXISTS (SELECT 1 FROM dedup_relations r
                          WHERE r.relation_id=a.relation_id AND r.status='retracted')''',
                    (rule_version, current, current)).fetchall()
            for a, b in rows:
                todo.append(b if a == current else a)
        if len(visited) < 2:
            raise ValueError('confirmed same-release group is required')
        for member in sorted(visited):
            before = _active(conn, member)
            record_verified_origin(conn, member, origin_id=release_origin,
                                   method='confirmed_release', evidence_ref=evidence_ref)
            new = conn.execute('''SELECT assertion_id FROM origin_assertions
                WHERE record_id=? AND origin_id=? AND method='confirmed_release' AND evidence_ref=?''',
                (member, release_origin, evidence_ref)).fetchone()
            for assertion_id, _origin, method, _proof in before:
                if method.startswith('issuer_cik:'):
                    conn.execute('INSERT INTO origin_supersessions VALUES (?,?,?)',
                                 (assertion_id, new[0], evidence_ref))
    return tuple(sorted(visited))
