"""Migration CLI must verify pinned inputs before any store is opened."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from catalyst_data.ingestion import input_manifest as I
from catalyst_data.ingestion.collectors import alpaca
from catalyst_data.ingestion.storage import body_migration as CLI
from catalyst_data.retrieval import evidence_fts_projection, web_bodies
from catalyst_data.storage import (
    body_migration as M,
)
from catalyst_data.storage import (
    body_versions as V,
)
from catalyst_data.storage import (
    legacy_cohorts,
    legacy_import,
    snapshots,
    temporal_evidence,
)


def args(source, destination):
    return ['--source',str(source),'--kind','alpaca','--snapshot-observed-at','2026-10-23T18:00:00Z','--destination',str(destination)]


def pin(tmp_path, monkeypatch, arguments, source):
    files=[source,Path(CLI.__file__),Path(I.__file__),Path(M.__file__),Path(V.__file__),Path(CLI.timefields.__file__)]
    manifest=tmp_path/'manifest.json'
    env={k:os.environ.get(k) for k in CLI.ENVIRONMENT}
    manifest.write_text(json.dumps({'schema_version':1,'invocation':['-m','catalyst_data.ingestion.storage.body_migration',*arguments],
        'files':[{'path':str(p),'sha256':I.fingerprint(p)} for p in files],'environment':env}))
    monkeypatch.setenv('CATALYST_INPUT_MANIFEST',str(manifest))
    monkeypatch.setenv('CATALYST_INPUT_RECEIPT',str(tmp_path/'receipt.json'))


def test_missing_manifest_never_opens_source_or_destination(tmp_path,monkeypatch):
    assert 'CATALYST_RUNTIME_DATA_DIR' in CLI.ENVIRONMENT
    monkeypatch.delenv('CATALYST_INPUT_MANIFEST',raising=False)
    monkeypatch.setattr(CLI.sqlite3,'connect',lambda *a,**k: (_ for _ in ()).throw(AssertionError('opened before guard')))
    assert CLI.main(args(tmp_path/'source.db',tmp_path/'result.db')) == 2
    assert not (tmp_path/'result.db').exists()


def test_pinned_copy_preserves_source_and_prints_receipt_first(tmp_path,monkeypatch,capsys):
    source=tmp_path/'source.db';dest=tmp_path/'result.db'
    c=alpaca.connect(source)
    alpaca.store_news(c,[{'id':1,'created_at':'2026-10-22T00:00:00Z','updated_at':'2026-10-22T01:00:00Z','headline':'Test','content':'complete'}],'2026-10-22T02:00:00Z')
    c.commit();c.close()
    before=I.fingerprint(source)
    arguments=args(source,dest);pin(tmp_path,monkeypatch,arguments,source)
    assert CLI.main(arguments) == 0
    output=capsys.readouterr().out.splitlines()
    assert 'input_manifest' in json.loads(output[0])
    assert json.loads(output[-1])['result']['inserted_versions'] == 1
    assert I.fingerprint(source) == before
    with sqlite3.connect(dest) as c:
        assert c.execute('SELECT provenance FROM body_versions').fetchone()[0] == 'versioned'


def test_destination_is_never_overwritten(tmp_path,monkeypatch):
    source=tmp_path/'source.db';c=alpaca.connect(source);c.close()
    dest=tmp_path/'result.db';dest.write_bytes(b'keep')
    arguments=args(source,dest);pin(tmp_path,monkeypatch,arguments,source)
    assert CLI.main(arguments) == 2
    assert dest.read_bytes() == b'keep'


CAPTURED = '2026-10-23T18:00:00Z'
ELIGIBLE = '2026-03-02T20:00:00Z'
LEGACY_TEXT = 'original legacy chunk text'
LEGACY_TITLE = 'Original title'
LEGACY_BODY_SHA = 'b' * 64


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _legacy_source(path):
    conn = sqlite3.connect(path)
    conn.executescript(web_bodies.SCHEMA)
    conn.execute(
        'INSERT INTO web_body_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ('asset-1', 0, 'chunk-1', LEGACY_TEXT, _sha(LEGACY_TEXT), LEGACY_BODY_SHA,
         'https://example.test/article', LEGACY_TITLE, 'Example News', 'reported_news',
         ELIGIBLE, '2026-03-02T20:05:00Z', 'LIVE_REFETCH'),
    )
    conn.execute('INSERT INTO web_headlines VALUES (?,?,?)', ('asset-1', 'ACME', ELIGIBLE))
    conn.commit()
    conn.close()
    return path


def _legacy_artifacts(tmp_path, source, *, count=1, source_sha=None, captured=CAPTURED,
                      text_sha=None):
    membership = tmp_path / 'membership.jsonl'
    record = {
        'source_id': 'daily:legacy', 'document_id': 'document:chunk-1',
        'evidence_id': 'evidence:article', 'kind': 'body', 'tickers': ['ACME'],
        'evidence_role': 'evidence', 'eligible_at': ELIGIBLE,
        'representation': 'legacy_chunk', 'text_sha256': text_sha or _sha(LEGACY_TEXT),
        'title_sha256': _sha(LEGACY_TITLE), 'asset_id': 'asset-1', 'chunk_id': 'chunk-1',
        'ordinal': 0, 'original_body_sha256': LEGACY_BODY_SHA,
    }
    membership.write_text(json.dumps(record, separators=(',', ':')) + '\n')
    source_sha = source_sha or I.fingerprint(source)
    catalog = tmp_path / 'legacy-catalog.json'
    catalog.write_text(json.dumps({
        'schema_version': 1,
        'cohort_id': 'daily-original-v1',
        'source_snapshot': {
            'store_id': 'catalog-declared-source-42', 'sha256': source_sha, 'captured_at': captured,
            'tables': ['web_body_chunks', 'web_headlines'],
        },
        'eligibility_rule': {'id': 'daily-original-rule', 'version': 'v1'},
        'correction_tables': [],
        'membership': {
            'path': str(membership), 'sha256': I.fingerprint(membership), 'record_count': count,
        },
    }, separators=(',', ':')))
    return catalog, membership


def _legacy_args(source, destination, catalog=None, *, cohort='daily-original-v1',
                 captured=CAPTURED, audit_only=False):
    result = ['--source', str(source), '--kind', 'legacy_chunks',
              '--snapshot-observed-at', captured]
    if audit_only:
        result.append('--audit-only')
    else:
        result += ['--destination', str(destination)]
    if catalog is not None:
        result += ['--legacy-catalog', str(catalog)]
    if cohort is not None:
        result += ['--legacy-cohort', cohort]
    return result


def _pin_legacy(tmp_path, monkeypatch, arguments, artifacts, *, exclude=(), receipt_name='receipt.json'):
    files = [
        *artifacts,
        Path(CLI.__file__), Path(I.__file__), Path(M.__file__), Path(V.__file__),
        Path(CLI.timefields.__file__), Path(legacy_import.__file__),
        Path(legacy_cohorts.__file__), Path(snapshots.__file__),
        Path(temporal_evidence.__file__), Path(evidence_fts_projection.__file__),
        Path(web_bodies.__file__),
    ]
    excluded = {Path(path).resolve() for path in exclude}
    files = list(dict.fromkeys(Path(path).resolve() for path in files if Path(path).resolve() not in excluded))
    manifest = tmp_path / 'manifest.json'
    env = {key: os.environ.get(key) for key in CLI.ENVIRONMENT}
    manifest.write_text(json.dumps({
        'schema_version': 1,
        'invocation': ['-m', 'catalyst_data.ingestion.storage.body_migration', *arguments],
        'files': [{'path': str(path), 'sha256': I.fingerprint(path)} for path in files],
        'environment': env,
    }))
    monkeypatch.setenv('CATALYST_INPUT_MANIFEST', str(manifest))
    monkeypatch.setenv('CATALYST_INPUT_RECEIPT', str(tmp_path / receipt_name))


def _last_receipt(capsys):
    return json.loads(capsys.readouterr().out.splitlines()[-1])


def test_pinned_legacy_catalog_import_is_copy_only_and_auditable(tmp_path, monkeypatch, capsys):
    source = _legacy_source(tmp_path / 'source.sqlite3')
    catalog, membership = _legacy_artifacts(tmp_path, source)
    destination = tmp_path / 'legacy-import.sqlite3'
    arguments = _legacy_args(source, destination, catalog)
    _pin_legacy(tmp_path, monkeypatch, arguments, (source, catalog, membership))
    source_sha = I.fingerprint(source)

    assert CLI.main(arguments) == 0
    receipt = _last_receipt(capsys)
    assert receipt['status'] == 'OK'
    assert receipt['kind'] == 'legacy_chunks'
    result = receipt['result']
    assert result['integrity_check'] == 'ok'
    assert result['source_sha256_before'] == result['source_sha256_after'] == source_sha
    assert result['counts'] == {
        'body_versions': 1,
        'legacy_version_metadata': 1,
        'temporal_evidence_map': 1,
        'evidence_text_projection': 1,
    }
    with sqlite3.connect(destination) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert conn.execute('SELECT provenance,observed_at,published_at,provider_updated_at FROM body_versions').fetchone() == (
            'legacy', '2026-10-23T18:00:00.000000+00:00', None, None,
        )
        assert conn.execute('SELECT body FROM body_versions').fetchone() == (LEGACY_TEXT,)
    assert result['role_policy_approved'] is False
    assert I.fingerprint(source) == source_sha

    destination_sha = I.fingerprint(destination)
    _pin_legacy(tmp_path, monkeypatch, arguments, (source, catalog, membership),
                receipt_name='repeat-receipt.json')
    assert CLI.main(arguments) == 2
    assert _last_receipt(capsys)['status'] == 'ERROR'
    assert I.fingerprint(destination) == destination_sha


def test_legacy_chunk_import_requires_an_explicit_pinned_catalog(tmp_path, monkeypatch, capsys):
    source = _legacy_source(tmp_path / 'source.sqlite3')
    catalog, membership = _legacy_artifacts(tmp_path, source)
    destination = tmp_path / 'legacy-import.sqlite3'
    arguments = _legacy_args(source, destination, catalog=None)
    _pin_legacy(tmp_path, monkeypatch, arguments, (source, catalog, membership))

    assert CLI.main(arguments) == 2
    assert _last_receipt(capsys)['status'] == 'ERROR'
    assert not destination.exists()


def test_unpinned_catalog_is_rejected_before_destination_creation(tmp_path, monkeypatch, capsys):
    source = _legacy_source(tmp_path / 'source.sqlite3')
    catalog, membership = _legacy_artifacts(tmp_path, source)
    destination = tmp_path / 'legacy-import.sqlite3'
    arguments = _legacy_args(source, destination, catalog)
    _pin_legacy(tmp_path, monkeypatch, arguments, (source, catalog, membership), exclude=(catalog,))

    assert CLI.main(arguments) == 2
    assert _last_receipt(capsys)['status'] == 'ERROR'
    assert not destination.exists()


@pytest.mark.parametrize('failure', ['membership_count', 'source_sha', 'snapshot_time', 'cohort_id'])
def test_legacy_preflight_mismatches_fail_before_destination_creation(
        tmp_path, monkeypatch, capsys, failure):
    source = _legacy_source(tmp_path / 'source.sqlite3')
    changes = {}
    captured = CAPTURED
    cohort_id = 'daily-original-v1'
    if failure == 'membership_count':
        changes['count'] = 2
    elif failure == 'source_sha':
        changes['source_sha'] = '0' * 64
    elif failure == 'snapshot_time':
        captured = '2026-10-23T19:00:00Z'
    else:
        cohort_id = 'different-cohort'
    catalog, membership = _legacy_artifacts(tmp_path, source, **changes)
    destination = tmp_path / 'legacy-import.sqlite3'
    arguments = _legacy_args(source, destination, catalog, cohort=cohort_id, captured=captured)
    _pin_legacy(tmp_path, monkeypatch, arguments, (source, catalog, membership))

    assert CLI.main(arguments) == 2
    assert _last_receipt(capsys)['status'] == 'ERROR'
    assert not destination.exists()


def test_legacy_catalog_import_failure_leaves_a_new_auditable_target_and_source_unchanged(
        tmp_path, monkeypatch, capsys):
    source = _legacy_source(tmp_path / 'source.sqlite3')
    catalog, membership = _legacy_artifacts(tmp_path, source, text_sha='a' * 64)
    destination = tmp_path / 'failed-import.sqlite3'
    arguments = _legacy_args(source, destination, catalog)
    _pin_legacy(tmp_path, monkeypatch, arguments, (source, catalog, membership))
    source_sha = I.fingerprint(source)

    assert CLI.main(arguments) == 2
    assert _last_receipt(capsys)['status'] == 'ERROR'
    assert destination.is_file()
    assert I.fingerprint(source) == source_sha
    with sqlite3.connect(destination) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone() == ('ok',)


def test_audit_only_legacy_cohort_flag_still_only_inventories(tmp_path, monkeypatch, capsys):
    source = _legacy_source(tmp_path / 'source.sqlite3')
    destination = tmp_path / 'unused.sqlite3'
    arguments = _legacy_args(source, destination, catalog=None, cohort='operator-label', audit_only=True)
    _pin_legacy(tmp_path, monkeypatch, arguments, (source,))

    assert CLI.main(arguments) == 0
    receipt = _last_receipt(capsys)
    assert receipt['legacy_cohort'] == 'operator-label'
    assert receipt['result']['chunks'] == 1
    assert receipt['result']['classification_applied'] is False
    assert receipt['result']['exact_distinct_lost_bodies'] is None
    assert not destination.exists()
