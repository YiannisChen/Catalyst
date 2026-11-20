import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from catalyst_data.retrieval import evidence_fts_projection
from catalyst_data.retrieval.evidence_search import search_stores
from catalyst_data.storage import body_versions, temporal_evidence
from catalyst_data.storage.snapshots import load_verified_stores


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _store(path, doc: str, text: str, observed: str) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    body_versions.ensure_schema(conn)
    temporal_evidence.ensure_schema(conn)
    evidence_fts_projection.ensure_schema(conn)
    at = _dt(observed)
    version_id = body_versions.put_version(
        conn, source_id="archive", document_id=doc, title=f"{doc} results",
        body=text, observed_at=at, published_at=at, provider_updated_at=at,
    )
    temporal_evidence.register_version(
        conn, version_id=version_id, ticker="ACME", kind="body",
        evidence_id=f"article:{doc}", evidence_role="evidence", association_observed_at=at,
        published_at_source="api", published_at_precision="second", api_published=observed,
    )
    evidence_fts_projection.project_evidence_text(
        conn, [evidence_fts_projection.EvidenceText(
            evidence_id=f"article:{doc}", version_id=version_id,
            source_id="archive", document_id=doc, provider="alpaca", kind="body",
            evidence_class="body_evidence", title=f"{doc} results", text=text,
            origin=None, provenance="versioned", observed_at=at.isoformat(),
            published_at=at.isoformat(), provider_updated_at=at.isoformat(),
        )],
    )
    conn.commit()
    conn.close()


def test_search_stores_matches_only_as_of_versions_and_audits_snapshot_ids(tmp_path):
    old = tmp_path / "old.sqlite3"
    current = tmp_path / "current.sqlite3"
    _store(old, "old", "Acme entered a new market", "2026-10-20T09:00:00Z")
    _store(current, "current", "Acme released a new product", "2026-10-20T11:00:00Z")

    result = search_stores(
        _seal(tmp_path, {"archive:old": old, "archive:current": current}),
        "ACME", '"Acme"', _dt("2026-10-20T00:00:00Z"), _dt("2026-10-21T00:00:00Z"),
        _dt("2026-10-20T10:00:00Z"), mode="observed", correction_table_id="corr:v1",
    )

    assert [row["document_id"] for row in result["results"]] == ["old"]
    assert result["results"][0]["store_id"] == "archive:old"
    assert result["audit"]["data_snapshot_ids"] == {
        "archive:current": _sha(current), "archive:old": _sha(old),
    }
    assert result["audit"]["result_count"] == 1


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seal(tmp_path, paths, extra_files=()):
    manifest = tmp_path / "inputs.json"
    manifest.write_text(json.dumps(dict(schema_version=1, invocation=["search"], environment={},
        files=[dict(path=str(path),sha256=_sha(path)) for path in (*paths.values(), *extra_files)])))
    return load_verified_stores(manifest, invocation=["search"], required_stores=paths, environ={})


def test_same_version_in_two_stores_resolves_in_each_own_snapshot(tmp_path):
    first, second = tmp_path / 'a.sqlite3', tmp_path / 'b.sqlite3'
    _store(first, 'shared', 'Acme official result', '2026-10-20T09:00:00Z')
    second.write_bytes(first.read_bytes())
    result = search_stores(_seal(tmp_path, {'a':first,'b':second}), 'ACME', 'Acme',
        _dt('2026-10-20T00:00:00Z'), _dt('2026-10-21T00:00:00Z'), _dt('2026-10-20T10:00:00Z'),
        mode='observed', correction_table_id='none')
    assert {row['store_id'] for row in result['results']} == {'a','b'}
    assert len(result['results']) == 2


def test_corrupt_projection_in_one_store_cannot_borrow_other_store_text(tmp_path):
    from catalyst_data.retrieval.evidence_search import EvidenceSearchError
    first, second = tmp_path / 'a.sqlite3', tmp_path / 'b.sqlite3'
    _store(first, 'shared', 'Acme official result', '2026-10-20T09:00:00Z')
    second.write_bytes(first.read_bytes())
    with sqlite3.connect(second) as conn:
        # Simulate a damaged imported index, bypassing normal append-only writes.
        conn.execute("DROP TRIGGER evidence_text_projection_no_update")
        conn.execute("UPDATE evidence_text_projection SET text='Acme altered projection'")
    with pytest.raises(EvidenceSearchError):
        search_stores(_seal(tmp_path, {'a':first,'b':second}), 'ACME', 'Acme',
            _dt('2026-10-20T00:00:00Z'), _dt('2026-10-21T00:00:00Z'), _dt('2026-10-20T10:00:00Z'),
            mode='observed', correction_table_id='none')


@pytest.mark.parametrize('field,value', [
    ('source_id','wrong-source'), ('document_id','wrong-document'), ('kind','filing'),
    ('evidence_class','filing_evidence'),
])
def test_projection_identity_or_class_corruption_is_an_error(tmp_path, field, value):
    from catalyst_data.retrieval.evidence_search import EvidenceSearchError
    path = tmp_path / 'corrupt.sqlite3'
    _store(path, 'shared', 'Acme official result', '2026-10-20T09:00:00Z')
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TRIGGER evidence_text_projection_no_update')
        # The parametrized column names are a fixed test allowlist.
        conn.execute(f'UPDATE evidence_text_projection SET {field}=?', (value,))
    with pytest.raises(EvidenceSearchError):
        search_stores(_seal(tmp_path, {'daily':path}), 'ACME', 'Acme',
            _dt('2026-10-20T00:00:00Z'), _dt('2026-10-21T00:00:00Z'), _dt('2026-10-20T10:00:00Z'),
            mode='observed', correction_table_id='none')


def test_search_explicit_body_store_selection_retains_auxiliary_input_pins(tmp_path):
    body = tmp_path / 'body.sqlite3'
    auxiliary = tmp_path / 'auxiliary.sqlite3'
    _store(body, 'shared', 'Acme official result', '2026-10-20T09:00:00Z')
    with sqlite3.connect(auxiliary) as conn:
        conn.execute('CREATE TABLE source_marker (id TEXT)')
    result = search_stores(_seal(tmp_path, {'body':body,'aux':auxiliary}), 'ACME', 'Acme',
        _dt('2026-10-20T00:00:00Z'), _dt('2026-10-21T00:00:00Z'), _dt('2026-10-20T10:00:00Z'),
        mode='observed', correction_table_id='none', store_ids=('body',))
    assert [row['store_id'] for row in result['results']] == ['body']
    assert result['audit']['input_snapshot_ids']['aux'] == _sha(auxiliary)



def test_cross_store_legacy_search_requires_pinned_catalog(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    observed = _dt('2026-10-22T10:00:00Z')
    eligible = _dt('2026-10-20T09:00:00Z')
    title, body = 'Legacy title', 'Acme legacy filing'
    with sqlite3.connect(path) as conn:
        body_versions.ensure_schema(conn)
        temporal_evidence.ensure_schema(conn)
        evidence_fts_projection.ensure_schema(conn)
        version_id = body_versions.put_version(conn, source_id='legacy', document_id='doc',
            title=title, body=body, observed_at=observed, provenance='legacy', legacy_cohort='old',
            legacy_eligible_at=eligible)
        temporal_evidence.register_version(conn, version_id=version_id, ticker='ACME', kind='body',
            evidence_id='legacy-e', evidence_role='evidence', association_observed_at=observed,
            eligible_at=eligible)
        evidence_fts_projection.project_evidence_text(conn, [evidence_fts_projection.EvidenceText(
            evidence_id='legacy-e', version_id=version_id, source_id='legacy', document_id='doc',
            provider='legacy', kind='body', evidence_class='body_evidence', title=title, text=body,
            origin=None, provenance='legacy', observed_at=observed.isoformat(),
            published_at=None, provider_updated_at=None)])
    members = tmp_path / 'members.jsonl'
    members.write_text(json.dumps(dict(source_id='legacy', document_id='doc', evidence_id='legacy-e',
        kind='body', tickers=['ACME'], evidence_role='evidence', eligible_at=eligible.isoformat(),
        representation='raw_full_body', text_sha256=hashlib.sha256(body.encode()).hexdigest(),
        title_sha256=hashlib.sha256(title.encode()).hexdigest()))+'\n')
    catalog = tmp_path / 'catalog.json'
    catalog.write_text(json.dumps(dict(schema_version=1, cohort_id='old',
        source_snapshot=dict(store_id='daily', sha256=_sha(path), captured_at=observed.isoformat(),
                             tables=['body_versions']),
        eligibility_rule=dict(id='old-rule', version='1'), correction_tables=[],
        membership=dict(path=str(members), sha256=_sha(members), record_count=1))))
    verified = _seal(tmp_path, {'daily':path}, extra_files=(members,catalog))
    options = dict(mode='published', correction_table_id='none', allowed_legacy_cohorts=('old',))
    args = (verified, 'ACME', 'Acme', _dt('2026-10-20T00:00:00Z'),
            _dt('2026-10-21T00:00:00Z'), _dt('2026-10-20T10:00:00Z'))
    with pytest.raises(ValueError, match='temporal'):
        search_stores(*args, **options)
    result = search_stores(*args, **options, legacy_catalogs={'old':catalog})
    assert [row['version_id'] for row in result['results']] == [version_id]
    assert result['results'][0]['binding_scope'] == 'catalog_only'
    assert result['audit']['legacy_catalogs']['old']['catalog_sha256'] == _sha(catalog)
