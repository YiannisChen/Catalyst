import sqlite3

import pytest
from catalyst_data.dedup import links
from catalyst_data.dedup.origins import (
    ensure_schema,
    record_issuer,
    record_verified_origin,
    resolve_origins,
    supersede_release_group,
)


def test_verified_filing_cik_is_canonical_and_distinct_filings_count_once():
    conn = sqlite3.connect(':memory:')
    record_issuer(conn, 'filing-1', cik='123', identity_source='filing', evidence_ref='sec:one')
    record_issuer(conn, 'filing-2', cik='0000000123', identity_source='accession_registry', evidence_ref='sec:two')
    result = resolve_origins(conn, ['filing-1', 'filing-2', 'unknown'])
    assert result['origin_ids'] == ['issuer:cik:0000000123']
    assert result['independent_origin_count'] == 1
    assert result['unresolved_record_ids'] == ['unknown']
    with pytest.raises(ValueError, match='source|CIK|identity'):
        record_issuer(conn, 'bad', cik='123', identity_source='current_ticker', evidence_ref='lookup')


def test_old_raw_origin_strings_are_not_counted_without_verified_assertion():
    conn = sqlite3.connect(':memory:')
    ensure_schema(conn)
    links.append_origin_assertion(conn, 'a', origin_id='filing:123', method='legacy')
    links.append_origin_assertion(conn, 'b', origin_id='publisher:host.example', method='legacy')
    result = resolve_origins(conn, ['a', 'b'])
    assert result['independent_origin_count'] == 0
    assert result['unresolved_record_ids'] == ['a', 'b']


def test_release_supersedes_issuer_assertions_for_every_member_atomically():
    conn = sqlite3.connect(':memory:')
    for name in ('sec', 'wire', 'ir'):
        record_issuer(conn, name, cik='123', identity_source='filing', evidence_ref='filing:'+name)
    for left, right in [('sec','wire'), ('wire','ir')]:
        a = links.EvidenceRecord(left, 'source', release_id='release-1', release_namespace='issuer', release_id_confirmed=True)
        b = links.EvidenceRecord(right, 'source', release_id='release-1', release_namespace='issuer', release_id_confirmed=True)
        links.persist_pair_links(conn, a, b)
    supersede_release_group(conn, 'sec', release_origin='release:issuer:release-1', evidence_ref='verified:release-1')
    result = resolve_origins(conn, ['sec', 'wire', 'ir'])
    assert result['origin_ids'] == ['release:issuer:release-1']
    assert result['independent_origin_count'] == 1
    assert not result['conflict_record_ids'] and not result['unresolved_record_ids']
    assert conn.execute('SELECT COUNT(*) FROM origin_supersessions').fetchone()[0] == 3


def test_conflicting_verified_origins_remain_unresolved():
    conn = sqlite3.connect(':memory:')
    record_verified_origin(conn, 'r', origin_id='publisher:one', method='confirmed_publisher', evidence_ref='proof:one')
    record_verified_origin(conn, 'r', origin_id='publisher:two', method='confirmed_publisher', evidence_ref='proof:two')
    result = resolve_origins(conn, ['r'])
    assert result['independent_origin_count'] == 0
    assert result['conflict_record_ids'] == ['r']


def test_group_replacement_failure_rolls_back_all_assertions():
    conn = sqlite3.connect(':memory:')
    for name in ('sec', 'wire'):
        record_issuer(conn, name, cik='123', identity_source='filing', evidence_ref='filing:'+name)
    links.persist_pair_links(conn,
        links.EvidenceRecord('sec', 'source', release_id='r', release_namespace='n', release_id_confirmed=True),
        links.EvidenceRecord('wire', 'source', release_id='r', release_namespace='n', release_id_confirmed=True))
    conn.execute("CREATE TRIGGER fail_wire BEFORE INSERT ON origin_assertions WHEN NEW.record_id='wire' AND NEW.method='confirmed_release' BEGIN SELECT RAISE(ABORT,'injected'); END")
    before = resolve_origins(conn, ['sec','wire'])
    with pytest.raises(sqlite3.IntegrityError):
        supersede_release_group(conn, 'sec', release_origin='release:n:r', evidence_ref='proof:r')
    assert resolve_origins(conn, ['sec','wire']) == before


def test_resolve_existing_origin_catalog_on_read_only_snapshot(tmp_path):
    path = tmp_path / 'origins.sqlite3'
    conn = sqlite3.connect(path)
    record_issuer(conn, 'filing', cik='123', identity_source='filing', evidence_ref='sec:filing')
    conn.commit()
    conn.close()
    reader = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    reader.execute('PRAGMA query_only=ON')
    assert resolve_origins(reader, ['filing'])['origin_ids'] == ['issuer:cik:0000000123']
    assert not reader.in_transaction


def test_supersessions_reject_cross_record_and_cycles():
    conn = sqlite3.connect(':memory:')
    record_issuer(conn, 'a', cik='123', identity_source='filing', evidence_ref='proof:a')
    record_verified_origin(conn, 'a', origin_id='release:n:r', method='confirmed_release', evidence_ref='proof:r')
    record_issuer(conn, 'b', cik='456', identity_source='filing', evidence_ref='proof:b')
    a_old = conn.execute("SELECT assertion_id FROM origin_assertions WHERE record_id='a' AND method LIKE 'issuer_cik:%'").fetchone()[0]
    a_new = conn.execute("SELECT assertion_id FROM origin_assertions WHERE record_id='a' AND method='confirmed_release'").fetchone()[0]
    b_old = conn.execute("SELECT assertion_id FROM origin_assertions WHERE record_id='b'").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('INSERT INTO origin_supersessions VALUES (?,?,?)', (a_old, b_old, 'bad'))
    conn.execute('INSERT INTO origin_supersessions VALUES (?,?,?)', (a_old, a_new, 'proof'))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('INSERT INTO origin_supersessions VALUES (?,?,?)', (a_new, a_old, 'cycle'))


def test_supersession_requires_evidence_reference():
    conn = sqlite3.connect(':memory:')
    record_issuer(conn, 'a', cik='123', identity_source='filing', evidence_ref='filing')
    record_verified_origin(conn, 'a', origin_id='release:n:r', method='confirmed_release', evidence_ref='proof')
    ids = [row[0] for row in conn.execute('SELECT assertion_id FROM origin_assertions ORDER BY method')]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('INSERT INTO origin_supersessions VALUES (?,?,?)', (*ids, '   '))


def test_read_rejects_corrupt_supersession_in_an_imported_catalog():
    conn = sqlite3.connect(':memory:')
    record_issuer(conn, 'a', cik='123', identity_source='filing', evidence_ref='proof:a')
    record_issuer(conn, 'b', cik='456', identity_source='filing', evidence_ref='proof:b')
    ids = dict(conn.execute('SELECT record_id,assertion_id FROM origin_assertions'))
    conn.execute('DROP TRIGGER origin_supersessions_validate')
    conn.execute('INSERT INTO origin_supersessions VALUES (?,?,?)', (ids['a'],ids['b'],'invalid'))
    with pytest.raises(ValueError, match='supersession'):
        resolve_origins(conn, ['a'])


def test_origin_counts_use_one_consistent_read_snapshot(tmp_path, monkeypatch):
    from catalyst_data.dedup import origins
    path = tmp_path / 'origin-wal.sqlite3'
    writer = sqlite3.connect(path)
    writer.execute('PRAGMA journal_mode=WAL')
    record_issuer(writer, 'a', cik='1', identity_source='filing', evidence_ref='a')
    record_issuer(writer, 'b', cik='2', identity_source='filing', evidence_ref='b')
    writer.commit()
    reader = sqlite3.connect(path)
    original = origins._active
    injected = False
    def change_after_first_read(conn, record_id):
        nonlocal injected
        rows = original(conn, record_id)
        if not injected:
            injected = True
            record_issuer(writer, 'b', cik='3', identity_source='filing', evidence_ref='new')
            writer.commit()
        return rows
    monkeypatch.setattr(origins, '_active', change_after_first_read)
    result = resolve_origins(reader, ['a','b'])
    assert result['origin_ids'] == ['issuer:cik:0000000001', 'issuer:cik:0000000002']
    assert not result['conflict_record_ids']
    assert not reader.in_transaction
    reader.close()
    writer.close()


def test_release_transition_does_not_hide_a_conflicting_publisher():
    conn = sqlite3.connect(':memory:')
    record_issuer(conn, 'sec', cik='123', identity_source='filing', evidence_ref='filing')
    record_verified_origin(conn, 'wire', origin_id='publisher:other', method='confirmed_publisher', evidence_ref='other-proof')
    links.persist_pair_links(conn,
        links.EvidenceRecord('sec', 's', release_id='r', release_namespace='n', release_id_confirmed=True),
        links.EvidenceRecord('wire', 's', release_id='r', release_namespace='n', release_id_confirmed=True))
    supersede_release_group(conn, 'sec', release_origin='release:n:r', evidence_ref='release-proof')
    result = resolve_origins(conn, ['sec','wire'])
    assert result['records']['sec']['origin_id'] == 'release:n:r'
    assert result['records']['wire']['status'] == 'conflict'
    assert result['conflict_record_ids'] == ['wire']
