"""Time-eligible FTS reads over synthetic, in-memory evidence only."""
import hashlib
import json
import sqlite3
from datetime import datetime

import pytest
from catalyst_data.retrieval import evidence_fts_projection as P
from catalyst_data.retrieval import evidence_search as S
from catalyst_data.storage import body_versions as B
from catalyst_data.storage import snapshots
from catalyst_data.storage import temporal_evidence as T


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    B.ensure_schema(db)
    T.ensure_schema(db)
    P.ensure_schema(db)
    yield db
    db.close()


def add(conn, doc="article", *, body="contract revenue", kind="body", role="evidence",
        observed="2026-10-20T10:00:00Z", first_seen=None, updated="2026-10-20T09:30:00Z",
        projected_text=None):
    published = "2026-10-20T09:00:00Z"
    title = "Title " + doc
    version = B.put_version(conn, source_id="synthetic", document_id=doc, title=title, body=body,
                            observed_at=dt(observed), first_seen_at=dt(first_seen) if first_seen else None,
                            published_at=dt(published), provider_updated_at=dt(updated) if updated else None)
    evidence_id = "web:" + doc
    T.register_version(conn, version_id=version, ticker="XYZ", kind=kind,
                       evidence_id=evidence_id, evidence_role=role,
                       association_observed_at=dt(observed), published_at_source="api",
                       published_at_precision="second", provider="synthetic", origin="publisher:synthetic")
    # Even a projected row explicitly labeled evidence cannot override a lead
    # role in the authoritative temporal map.
    P.project_evidence_text(conn, [P.EvidenceText(
        evidence_id=evidence_id, version_id=version, source_id="synthetic", document_id=doc,
        provider="synthetic", kind=kind, evidence_class="body_evidence" if kind == "body" else "filing_evidence",
        title=title, text=body if projected_text is None else projected_text,
        origin="publisher:synthetic", provenance="versioned", observed_at=observed,
        published_at=published, provider_updated_at=updated)])
    return version


def search(conn, query="contract", **kwargs):
    options = dict(mode="observed", data_snapshot_id="snapshot:test", correction_table_id="corrections:test")
    options.update(kwargs)
    return S.search(conn, "XYZ", query, dt("2026-10-20T00:00:00Z"), dt("2026-10-21T00:00:00Z"),
                    dt("2026-10-20T10:00:00Z"), **options)


def pin_legacy_search_catalog(tmp_path, conn, version_id, *, cohort_id="old", pin_catalog=True):
    source_path = tmp_path / "legacy-search-source.sqlite3"
    conn.commit()
    target = sqlite3.connect(source_path)
    conn.backup(target)
    target.close()
    source = sqlite3.connect(source_path)
    row = source.execute(
        "SELECT v.source_id,v.document_id,v.title,v.body,v.legacy_eligible_at,m.ticker,m.kind,"
        "m.evidence_id,m.evidence_role,v.observed_at,d.first_seen_at,m.association_observed_at "
        "FROM body_versions v JOIN body_documents d USING(source_id,document_id) "
        "JOIN temporal_evidence_map m USING(version_id) WHERE v.version_id=?", (version_id,),
    ).fetchone()
    (source_id, document_id, title, body, eligible, ticker, kind, evidence_id, role,
     observed, first_seen, associated) = row
    source.close()
    membership_path = tmp_path / "legacy-search-members.jsonl"
    membership_path.write_text(json.dumps({
        "source_id": source_id, "document_id": document_id, "evidence_id": evidence_id,
        "kind": kind, "tickers": [ticker], "evidence_role": role, "eligible_at": eligible,
        "representation": "raw_full_body",
        "text_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "title_sha256": hashlib.sha256(title.encode()).hexdigest(),
    }, separators=(",", ":")) + "\n")
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    catalog_path = tmp_path / "legacy-search-catalog.json"
    catalog_path.write_text(json.dumps({
        "schema_version": 1, "cohort_id": cohort_id,
        "source_snapshot": {"store_id": "legacy-search-source", "sha256": source_sha,
                            "captured_at": max(observed, first_seen, associated),
                            "tables": ["body_versions"]},
        "eligibility_rule": {"id": "legacy-search-rule", "version": "v1"},
        "correction_tables": [],
        "membership": {"path": str(membership_path),
                        "sha256": hashlib.sha256(membership_path.read_bytes()).hexdigest(),
                        "record_count": 1},
    }, separators=(",", ":")))
    files = [source_path]
    if pin_catalog:
        files.extend((catalog_path, membership_path))
    manifest_path = tmp_path / "legacy-search-manifest.json"
    manifest_path.write_text(json.dumps({
        "schema_version": 1, "invocation": ["evidence-search-legacy-test"],
        "environment": {},
        "files": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                  for path in files],
    }))
    verified = snapshots.load_verified_stores(
        manifest_path, invocation=("evidence-search-legacy-test",),
        required_stores={"legacy-search-source": source_path}, required_env=(), environ={},
    )
    read_conn = sqlite3.connect(source_path)
    return read_conn, verified, catalog_path


def test_observed_first_seen_and_version_cutoff_are_strict(conn):
    visible = add(conn, doc="visible", first_seen="2026-10-20T10:00:00Z")
    add(conn, doc="late", observed="2026-10-20T10:00:01Z", first_seen="2026-10-20T10:00:01Z")
    assert [r["version_id"] for r in search(conn)["results"]] == [visible]


def test_published_modified_after_cutoff_is_not_searchable_evidence(conn):
    visible = add(conn, doc="visible", observed="2026-10-23T10:00:00Z", updated="2026-10-20T10:00:00Z")
    add(conn, doc="modified", observed="2026-10-23T10:00:00Z", updated="2026-10-20T10:00:01Z")
    add(conn, doc="unknown", observed="2026-10-23T10:00:00Z", updated=None)
    result = search(conn, mode="published")
    assert [r["version_id"] for r in result["results"]] == [visible]
    assert result["audit"]["mode"] == "published"
    assert result["audit"]["label"] == "回溯查询"


def test_explicit_lead_cannot_be_promoted_by_projection(conn):
    add(conn, role="lead")
    assert search(conn)["results"] == []


def test_only_current_eligible_version_can_match(conn):
    add(conn, doc="revised", body="obsoleteword", observed="2026-10-20T08:00:00Z", updated="2026-10-20T07:00:00Z")
    latest = add(conn, doc="revised", body="currentword", observed="2026-10-20T09:00:00Z", updated="2026-10-20T08:00:00Z")
    assert search(conn, "obsoleteword")["results"] == []
    assert [r["version_id"] for r in search(conn, "currentword")["results"]] == [latest]


def test_filings_are_searchable(conn):
    filing = add(conn, kind="filing")
    assert [r["version_id"] for r in search(conn)["results"]] == [filing]


def test_no_matches_keeps_explicit_read_audit(conn):
    add(conn)
    result = search(conn, "absentword")
    assert result["results"] == []
    assert result["audit"]["data_snapshot_id"] == "snapshot:test"
    assert result["audit"]["correction_table_id"] == "corrections:test"
    assert result["audit"]["cutoff"] == "2026-10-20T10:00:00.000000+00:00"
    assert result["audit"]["window_start"] == "2026-10-20T00:00:00.000000+00:00"
    assert result["audit"]["sort_policy"] == "eligible_at_desc_identity_asc"
    assert result["audit"]["ranking"] == "chronological_not_relevance"


def test_limit_applied_after_temporal_intersection(conn):
    add(conn, doc="a-lead", role="lead")
    add(conn, doc="b-future", observed="2026-10-21T10:00:00Z")
    eligible = add(conn, doc="z-eligible")
    assert [r["version_id"] for r in search(conn, limit=1)["results"]] == [eligible]


def test_tied_matches_have_deterministic_identity_order(conn):
    add(conn, doc="z")
    add(conn, doc="a")
    assert [r["evidence_id"] for r in search(conn)["results"]] == ["web:a", "web:z"]
    assert [r["evidence_id"] for r in search(conn, limit=1)["results"]] == ["web:a"]


def test_returned_content_and_metadata_come_from_temporal_evidence(conn):
    add(conn)
    expected = T.as_of(conn, "XYZ", dt("2026-10-20T00:00:00Z"), dt("2026-10-21T00:00:00Z"),
                       dt("2026-10-20T10:00:00Z"), mode="observed", kinds=["body", "filing"],
                       data_snapshot_id="snapshot:test", correction_table_id="corrections:test")
    assert search(conn)["results"] == expected["evidence"]


@pytest.mark.parametrize("limit", [True, False, 0, -1, 1.5, "2", 201])
def test_invalid_limit_rejected(conn, limit):
    with pytest.raises(ValueError, match="limit"):
        search(conn, limit=limit)


@pytest.mark.parametrize("query", ["", "  ", None, 4])
def test_invalid_query_rejected(conn, query):
    with pytest.raises(ValueError, match="query"):
        search(conn, query)


def test_match_uses_bound_parameter_and_invalid_fts_does_not_run_sql(conn):
    add(conn)
    with pytest.raises(ValueError, match="query"):
        search(conn, "' OR 1=1; DROP TABLE body_versions; --")
    assert conn.execute("SELECT COUNT(*) FROM body_versions").fetchone()[0] == 1


def test_mode_is_required_and_validated(conn):
    add(conn)
    with pytest.raises(ValueError, match="mode"):
        search(conn, mode="automatic")
    with pytest.raises(TypeError):
        S.search(conn, "XYZ", "contract", dt("2026-10-20T00:00:00Z"), dt("2026-10-21T00:00:00Z"),
                 dt("2026-10-20T10:00:00Z"), data_snapshot_id="snapshot:test", correction_table_id="corrections:test")


@pytest.mark.parametrize("missing", ["evidence_text_fts", "evidence_text_projection"])
def test_empty_eligible_set_still_requires_readable_fts_schema(conn, missing):
    conn.execute("DROP TABLE " + missing)
    with pytest.raises(ValueError, match="schema"):
        search(conn)


def test_empty_eligible_set_still_validates_fts_expression(conn):
    with pytest.raises(S.EvidenceSearchError, match="query"):
        search(conn, '"unterminated')


def test_projection_kind_class_mismatch_fails_closed(conn):
    add(conn, doc="mismatched")
    conn.execute("DROP TRIGGER evidence_text_projection_no_update")
    conn.execute(
        "UPDATE evidence_text_projection SET evidence_class='filing_evidence' "
        "WHERE evidence_id='web:mismatched'"
    )

    with pytest.raises(S.EvidenceSearchError, match="evidence class"):
        search(conn, query="contract")


def test_single_store_search_requires_and_uses_pinned_legacy_catalog(conn, tmp_path):
    title, body = 'Legacy title', 'contract legacy body'
    eligible = dt('2026-10-20T09:00:00Z')
    observed = dt('2026-10-20T09:30:00Z')
    version = B.put_version(conn, source_id='legacy', document_id='d', title=title, body=body,
        observed_at=observed, provenance='legacy', legacy_cohort='old', legacy_eligible_at=eligible)
    T.register_version(conn, version_id=version, ticker='XYZ', kind='body', evidence_id='e',
        evidence_role='evidence', association_observed_at=observed, eligible_at=eligible)
    P.project_evidence_text(conn, [P.EvidenceText(evidence_id='e', version_id=version,
        source_id='legacy', document_id='d', provider='legacy', kind='body', evidence_class='body_evidence',
        title=title, text=body, origin=None, provenance='legacy', observed_at=observed.isoformat(),
        published_at=None, provider_updated_at=None)])
    read_conn, verified, catalog_path = pin_legacy_search_catalog(
        tmp_path, conn, version, cohort_id='old',
    )
    assert search(read_conn, mode='published', allowed_legacy_cohorts=('old',),
                  legacy_verified_stores=verified,
                  legacy_catalogs={'old': catalog_path})['results'][0]['version_id'] == version
    with pytest.raises(ValueError, match='catalog|cohort'):
        search(read_conn, mode='published', allowed_legacy_cohorts=('old',))


def test_single_store_search_rejects_unpinned_legacy_catalog(conn, tmp_path):
    title, body = 'Legacy title', 'contract legacy body'
    eligible = dt('2026-10-20T09:00:00Z')
    observed = dt('2026-10-22T12:00:00Z')
    version = B.put_version(conn, source_id='legacy', document_id='d', title=title, body=body,
        observed_at=observed, provenance='legacy', legacy_cohort='old', legacy_eligible_at=eligible)
    T.register_version(conn, version_id=version, ticker='XYZ', kind='body', evidence_id='e',
        evidence_role='evidence', association_observed_at=observed, eligible_at=eligible)
    P.project_evidence_text(conn, [P.EvidenceText(evidence_id='e', version_id=version,
        source_id='legacy', document_id='d', provider='legacy', kind='body', evidence_class='body_evidence',
        title=title, text=body, origin=None, provenance='legacy', observed_at=observed.isoformat(),
        published_at=None, provider_updated_at=None)])
    read_conn, verified, catalog_path = pin_legacy_search_catalog(
        tmp_path, conn, version, cohort_id='old', pin_catalog=False,
    )

    with pytest.raises(ValueError, match='catalog|pinned|manifest'):
        search(read_conn, mode='published', allowed_legacy_cohorts=('old',),
               legacy_verified_stores=verified, legacy_catalogs={'old': catalog_path})


def test_single_store_search_rejects_a_hand_constructed_legacy_cohort(conn):
    title, body = 'Legacy title', 'contract legacy body'
    eligible = dt('2026-10-20T09:00:00Z')
    observed = dt('2026-10-22T12:00:00Z')
    version = B.put_version(conn, source_id='legacy', document_id='d', title=title, body=body,
        observed_at=observed, provenance='legacy', legacy_cohort='old', legacy_eligible_at=eligible)
    T.register_version(conn, version_id=version, ticker='XYZ', kind='body', evidence_id='e',
        evidence_role='evidence', association_observed_at=observed, eligible_at=eligible)
    P.project_evidence_text(conn, [P.EvidenceText(evidence_id='e', version_id=version,
        source_id='legacy', document_id='d', provider='legacy', kind='body', evidence_class='body_evidence',
        title=title, text=body, origin=None, provenance='legacy', observed_at=observed.isoformat(),
        published_at=None, provider_updated_at=None)])
    with pytest.raises(TypeError):
        search(conn, mode='published', allowed_legacy_cohorts=('old',),
               legacy_cohorts=(object(),))


def test_single_store_search_rejects_catalog_only_raw_body_from_other_snapshot(conn, tmp_path):
    title, body = 'Legacy title', 'contract legacy body'
    eligible = dt('2026-10-20T09:00:00Z')
    observed = dt('2026-10-20T09:30:00Z')
    version = B.put_version(conn, source_id='legacy', document_id='d', title=title, body=body,
        observed_at=observed, provenance='legacy', legacy_cohort='old', legacy_eligible_at=eligible)
    T.register_version(conn, version_id=version, ticker='XYZ', kind='body', evidence_id='e',
        evidence_role='evidence', association_observed_at=observed, eligible_at=eligible)
    P.project_evidence_text(conn, [P.EvidenceText(evidence_id='e', version_id=version,
        source_id='legacy', document_id='d', provider='legacy', kind='body', evidence_class='body_evidence',
        title=title, text=body, origin=None, provenance='legacy', observed_at=observed.isoformat(),
        published_at=None, provider_updated_at=None)])
    pinned_conn, verified, catalog_path = pin_legacy_search_catalog(
        tmp_path, conn, version, cohort_id='old',
    )
    other_path = tmp_path / 'legacy-search-other.sqlite3'
    other = sqlite3.connect(other_path)
    pinned_conn.backup(other)
    add(other, doc='extra')
    other.commit()

    with pytest.raises((ValueError, S.EvidenceSearchError), match='snapshot|source|raw|catalog'):
        search(other, mode='published', allowed_legacy_cohorts=('old',),
               legacy_verified_stores=verified, legacy_catalogs={'old': catalog_path})
