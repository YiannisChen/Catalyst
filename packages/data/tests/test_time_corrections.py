"""Pinned correction decisions on synthetic CSVs; no real corpus access."""
import hashlib

import pytest
from catalyst_data.storage import time_corrections as C

NEWS = (b'url,publisher,stored_eligible_at,corrected_at,offset_h,basis,stores\n'
        b'https://example.com/a?utm_source=feed,Benzinga,2026-10-20T12:00:00Z,2026-10-20T16:00:00Z,-4,synthetic,eval+server\n')
SEC = (b'accession,cik,form,items,json_acceptance_utc,edgar_accepted_et,edgar_accepted_utc,json_minus_edgar_h,in_stores\n'
       b'0000000001-26-000001,1,8-K,,2026-10-20T20:00:00Z,,2026-10-20T16:00:00Z,4,server:news.sqlite3;dev.sqlite3\n'
       b'0000000001-26-000002,1,8-K,,2026-10-20T20:00:00Z,,2026-10-20T16:00:00Z,4,\n')


def load(data=NEWS, kind='finnhub', filename='time_corrections_v1.csv'):
    return C.load_corrections(data, kind=kind, filename=filename,
                              expected_sha256=hashlib.sha256(data).hexdigest())


def news(table, value='2026-10-20T12:00:00Z', **changes):
    args = dict(store='server', url='https://example.com/a', publisher=' benzinga ')
    args.update(changes)
    return table.apply(value, **args)


def test_news_canonical_identity_and_exact_store_membership():
    table = load()
    result = news(table)
    assert result.status == 'corrected'
    assert result.original_at == '2026-10-20T12:00:00.000000+00:00'
    assert result.corrected_at == result.effective_at == '2026-10-20T16:00:00.000000+00:00'
    assert result.table_id == table.table_id
    assert result.sha256 == hashlib.sha256(NEWS).hexdigest()
    assert table.table_id == 'time_corrections_v1:' + result.sha256
    assert result.time_flags == ('corrected:' + table.table_id,)
    assert news(table, store='serve').status == 'not_applicable'
    assert news(table, publisher='CNBC').status == 'unmatched'
    assert not news(table, store='serve').original_missing
    assert not news(table, publisher='CNBC').original_missing
    assert not result.original_missing


def test_lookup_is_scoped_and_does_not_guess_provider_or_publisher():
    table = load()
    assert table.lookup(store='eval', url='https://example.com/a', publisher='BENZINGA')
    assert table.lookup(store='other', url='https://example.com/a', publisher='Benzinga') is None
    with pytest.raises(ValueError):
        table.lookup(store='', url='https://example.com/a', publisher='Benzinga')


def test_apply_compares_instants_and_is_idempotent():
    table = load()
    assert news(table, '2026-10-20T08:00:00-04:00').status == 'corrected'
    result = news(table, '2026-10-20T12:00:00-04:00')
    assert result.status == 'already_corrected'
    assert result.time_flags == ('corrected:' + table.table_id,)


def test_conflict_does_not_replace_a_different_current_time():
    result = news(load(), '2026-10-20T13:00:00Z')
    assert result.status == 'conflict'
    assert result.effective_at == result.current_at == '2026-10-20T13:00:00.000000+00:00'
    assert result.original_at == '2026-10-20T12:00:00.000000+00:00'
    assert result.time_flags == ()


def test_sec_accession_is_exact_and_scope_tokens_are_not_split_on_colon():
    table = load(SEC, 'sec', 'sec_acceptance_v1.csv')
    args = dict(store='server:news.sqlite3', accession='0000000001-26-000001')
    assert table.apply('2026-10-20T20:00:00Z', **args).status == 'corrected'
    assert table.apply('2026-10-20T20:00:00Z', **{**args, 'store': 'server'}).status == 'not_applicable'
    assert table.apply('2026-10-20T20:00:00Z', **{**args, 'accession': '000000000126000001'}).status == 'unmatched'
    assert table.apply('2026-10-20T20:00:00Z', **{**args, 'accession': '0000000001-26-000002'}).status == 'not_applicable'


def test_sec_index_is_authoritative_even_when_json_original_is_missing():
    table = load(SEC.replace(b'2026-10-20T20:00:00Z', b''), 'sec', 'sec_acceptance_v1.csv')
    result = table.apply('2026-10-20T19:00:00Z', store='dev.sqlite3', accession='0000000001-26-000001')
    assert result.status == 'corrected'
    assert result.original_at is result.stored_at is None
    assert result.original_missing
    missing = table.apply(None, store='dev.sqlite3', accession='0000000001-26-000001')
    assert missing.current_at is missing.original_at is None
    assert missing.effective_at == result.effective_at
    assert missing.original_missing
    replay = table.apply(result.effective_at, store='dev.sqlite3', accession='0000000001-26-000001')
    assert replay.status == 'already_corrected'
    assert replay.original_missing
    assert result.current_at == '2026-10-20T19:00:00.000000+00:00'
    assert result.effective_at == '2026-10-20T16:00:00.000000+00:00'
    assert result.time_flags == ('corrected:' + table.table_id,)


def test_sec_authoritative_acceptance_can_replace_a_different_current_instant():
    table = load(SEC, 'sec', 'sec_acceptance_v1.csv')
    result = table.apply('2026-10-20T19:00:00Z', store='dev.sqlite3', accession='0000000001-26-000001')
    assert result.status == 'corrected'
    assert result.original_at == '2026-10-20T20:00:00.000000+00:00'
    assert result.effective_at == '2026-10-20T16:00:00.000000+00:00'


def test_sec_with_recorded_json_original_is_idempotent_on_corrected_replay():
    table = load(SEC, 'sec', 'sec_acceptance_v1.csv')
    args = dict(store='dev.sqlite3', accession='0000000001-26-000001')
    corrected = table.apply('2026-10-20T20:00:00Z', **args)
    replay = table.apply(corrected.effective_at, **args)
    assert corrected.status == 'corrected'
    assert replay.status == 'already_corrected'
    assert replay.effective_at == corrected.effective_at
    assert replay.original_at == corrected.original_at == '2026-10-20T20:00:00.000000+00:00'
    assert not replay.original_missing
    assert replay.time_flags == corrected.time_flags == ('corrected:' + table.table_id,)


def test_digest_required_and_verified_before_csv_parse():
    with pytest.raises(ValueError, match='SHA'):
        C.load_corrections(b'bad csv', kind='finnhub', filename='x.csv', expected_sha256='0' * 64)
    with pytest.raises(TypeError):
        C.load_corrections(NEWS, kind='finnhub', filename='x.csv')


def test_conflicting_canonical_duplicate_is_rejected():
    extra = b'https://example.com/a,BENZINGA,2026-10-20T12:00:00Z,2026-10-20T17:00:00Z,-5,synthetic,server\n'
    with pytest.raises(ValueError, match='conflicting'):
        load(NEWS + extra)


def test_same_scoped_canonical_identity_accepts_equivalent_offset_instants():
    duplicate = (b'https://example.com/a,BENZINGA,2026-10-20T08:00:00-04:00,'
                 b'2026-10-20T12:00:00-04:00,-4,synthetic,server\n')
    table = load(NEWS + duplicate)
    result = news(table)
    assert result.status == 'corrected'
    assert result.original_at == '2026-10-20T12:00:00.000000+00:00'
    assert result.effective_at == '2026-10-20T16:00:00.000000+00:00'
    assert result.time_flags == ('corrected:' + table.table_id,)
    assert news(table, '2026-10-20T08:00:00-04:00') == result


def test_duplicate_identity_with_disjoint_store_scopes_is_supported():
    data = NEWS.replace(b'eval+server', b'eval') + b'https://example.com/a,Benzinga,2026-10-20T12:00:00Z,2026-10-20T17:00:00Z,-5,synthetic,server\n'
    table = load(data)
    assert news(table).effective_at == '2026-10-20T17:00:00.000000+00:00'
    assert news(table, store='eval').effective_at == '2026-10-20T16:00:00.000000+00:00'


@pytest.mark.parametrize('value', ['2026-10-20T12:00:00', None, 'bad'])
def test_unaware_or_missing_current_time_is_rejected(value):
    with pytest.raises(ValueError):
        news(load(), value)


def test_malformed_table_timestamp_and_unknown_kind_are_rejected():
    with pytest.raises(ValueError):
        load(NEWS.replace(b'2026-10-20T16:00:00Z', b'2026-10-20T16:00:00'))
    with pytest.raises(ValueError):
        load(kind='other')


def test_invalid_csv_bytes_and_quoting_raise_value_error():
    for malformed in (
        b'\xff',
        b'url,publisher,stored_eligible_at,corrected_at,stores\n"unterminated',
    ):
        with pytest.raises(ValueError):
            C.load_corrections(
                malformed, kind="finnhub", filename="trial.csv",
                expected_sha256=hashlib.sha256(malformed).hexdigest(),
            )
