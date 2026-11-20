from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from catalyst_data.corpus import streaming_publication as publication
from catalyst_data.corpus.persisted_id import is_valid_persisted_document_id
from catalyst_data.ingestion.ops import daily
from catalyst_data.retrieval.fts5 import retrieve_lexical
from catalyst_data.storage import body_versions, temporal_evidence
from catalyst_data.storage.sqlite import ensure_filings_tables, init_db

SNAPSHOT_ID = "d" * 64
NOW = "2026-11-05T12:00:00Z"
STORE_ID = "server:news_bodies_v2.sqlite3"
ACCESSION = "0000000001-26-000001"
URL = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex991.htm"
CORRECTIONS = (
    b"accession,cik,form,items,json_acceptance_utc,edgar_accepted_et,edgar_accepted_utc,"
    b"json_minus_edgar_h,in_stores\n"
    b"0000000001-26-000001,1,8-K,2.02,2026-10-30T19:58:00Z,2026-10-30 16:05:00,"
    b"2026-10-30T20:05:00Z,-0.1,server:news_bodies_v2.sqlite3\n"
    b"0000000001-26-000004,1,8-K,2.02,2026-10-30T19:58:00Z,2026-10-30 16:05:00,"
    b"2026-10-30T20:05:00Z,-0.1,other-store\n"
)


def _corrections(tmp_path: Path) -> Path:
    path = tmp_path / "sec_acceptance_v1.csv"
    path.write_bytes(CORRECTIONS)
    return path


def _body_store(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    body_versions.ensure_schema(conn)
    temporal_evidence.ensure_schema(conn)
    return conn


def _add_filing(
    conn: sqlite3.Connection,
    accession: str,
    url: str,
    *,
    ticker: str = "AAA",
    eligible_at: str | None = "2026-10-30T19:58:00Z",
    published_at_source: str | None = "sec_index",
    title: str = "AAA Form 8-K EX-99.1: 8-K",
    body: str = "quartzhelium disclosure " * 140,
) -> str:
    eligible = datetime.fromisoformat(eligible_at.replace("Z", "+00:00")) if eligible_at else None
    observed = datetime(2026, 10, 31, tzinfo=timezone.utc)
    version_id = body_versions.put_version(
        conn, source_id="daily:sec", document_id=accession, title=title, body=body,
        observed_at=observed, published_at=eligible,
    )
    temporal_evidence.register_version(
        conn, version_id=version_id, ticker=ticker, kind="filing", evidence_id=f"sec:{accession}",
        evidence_role="evidence", association_observed_at=observed, eligible_at=eligible,
        published_at_source=published_at_source, published_at_precision="second",
        url=url, provider="sec", source_class="official_government", sec_accession=accession,
    )
    conn.commit()
    return version_id


def _snapshot(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    init_db(conn)
    ensure_filings_tables(conn)
    return conn


def _add_snapshot_document(
    conn: sqlite3.Connection,
    *,
    accession: str,
    url: str,
    is_canonical: int = 1,
    is_rag_eligible: int = 1,
    extraction_status: str = "success",
    text: str = "snapshot body text",
    document_id: str = "e" * 64,
) -> None:
    filing_id = "snap-" + hashlib.sha256(
        f"{accession}\x1f{url}\x1f{is_canonical}\x1f{is_rag_eligible}\x1f{extraction_status}".encode()
    ).hexdigest()
    conn.execute(
        """INSERT INTO filings
           (filing_id,cik,ticker,form_type,filed_at,accession_number,url,is_canonical,is_rag_eligible)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (filing_id, "0000000001", "AAA", "8-K", "2026-10-30", accession,
         url, is_canonical, is_rag_eligible),
    )
    conn.execute(
        """INSERT INTO filing_documents
           (filing_id,document_url,document_type,text,extraction_status,document_id)
           VALUES (?,?,?,?,?,?)""",
        (filing_id, url, "exhibit_99.1", text, extraction_status, document_id),
    )


def _seed_current_manifest(conn: sqlite3.Connection) -> None:
    content_hash = hashlib.sha256(b"baseline text").hexdigest()
    metadata_hash = hashlib.sha256(b"baseline metadata").hexdigest()
    manifest_id = "a" * 64
    conn.execute(
        "INSERT INTO corpus_manifest (manifest_id,manifest_json,is_current,created_at) VALUES (?,?,1,?)",
        (manifest_id, json.dumps({"certified_snapshot_identity": "0" * 64}), NOW),
    )
    conn.execute(
        """INSERT INTO corpus_chunks
           (chunk_id,document_id,chunk_profile_version,section_key,ordinal,content_text,
            content_hash,metadata_hash,source_class,available_at,ticker_associations,eligibility,
            manifest_id,status,boundary_kind,body_token_start,body_token_end,body_overlap_tokens,
            prefix_token_count,prefix_truncated,section_parse_degraded,created_at,updated_at)
           VALUES ('baseline:news_v2:body:0001','baseline','news_v2','body','0001',
                   'baseline text',?,?,'reported_news',?,'[\"AAA\"]','eligible',?,'active',
                   'document_end',0,2,0,0,0,0,?,?)""",
        (content_hash, metadata_hash, NOW, manifest_id, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO corpus_chunks_fts (manifest_id,chunk_id,content_text) VALUES (?,?,?)",
        (manifest_id, "baseline:news_v2:body:0001", "baseline text"),
    )
    conn.execute(
        """INSERT INTO lexical_index_state
           (singleton_id,schema_version,corpus_manifest_id,mode_served,row_count,built_at)
           VALUES (1,'1.0.0',?,'fts5',1,?)""",
        (manifest_id, NOW),
    )
    conn.commit()


def test_sec_body_stream_uses_corrected_acceptance_and_deduplicates_snapshot(tmp_path):
    body = _body_store(tmp_path / "news_bodies_v2.sqlite3")
    _add_filing(body, ACCESSION, URL)
    _add_filing(body, "0000000001-26-000002", URL.replace("ex991", "ex992"),
                eligible_at=None, body="missing acceptance text " * 50)
    _add_filing(body, "0000000001-26-000003", URL.replace("ex991", "ex993"),
                published_at_source="api", body="unverified source text " * 50)
    _add_filing(body, "0000000001-26-000004", URL.replace("ex991", "ex994"),
                body="not applicable correction text " * 50)
    snapshot = _snapshot(tmp_path / "snapshot.sqlite3")
    _add_snapshot_document(snapshot, accession=ACCESSION, url=URL)
    snapshot.commit()

    correction_path = _corrections(tmp_path)
    summary = publication.sec_bodies_snapshot_identity(
        body, snapshot, correction_table_path=correction_path, store_id=STORE_ID,
    )
    documents = list(publication._iter_sec_body_documents(
        body, snapshot, page_size=10, correction_table_path=correction_path, store_id=STORE_ID,
    ))

    assert summary.included_count == 0
    assert summary.duplicate_count == 1
    assert summary.excluded_count == 4
    assert all(source.document_id != publication._sec_body_document_id(ACCESSION, URL)
               for source in documents)
    missing_time = next(source for source in documents if "missing acceptance time" in source.ineligibility_reason)
    untrusted_source = next(source for source in documents if "verified EDGAR index-time" in source.ineligibility_reason)
    not_applicable = next(source for source in documents
                          if source.ineligibility_reason == "correction_not_applicable_store")
    assert {missing_time.eligibility, untrusted_source.eligibility, not_applicable.eligibility} == {"ineligible"}


def test_sec_body_document_ids_and_identity_are_stable_and_change_on_new_body(tmp_path):
    body = _body_store(tmp_path / "news_bodies_v2.sqlite3")
    _add_filing(body, ACCESSION, URL)
    snapshot = _snapshot(tmp_path / "snapshot.sqlite3")
    correction_path = _corrections(tmp_path)

    first = publication.sec_bodies_snapshot_identity(
        body, snapshot, correction_table_path=correction_path, store_id=STORE_ID,
    )
    again = publication.sec_bodies_snapshot_identity(
        body, snapshot, correction_table_path=correction_path, store_id=STORE_ID,
    )
    source = next(publication._iter_sec_body_documents(
        body, snapshot, page_size=10, correction_table_path=correction_path, store_id=STORE_ID,
    ))
    assert source.document_id == publication._sec_body_document_id(ACCESSION, URL)
    assert is_valid_persisted_document_id(source.document_id)
    assert first.identity == again.identity
    assert first.included_count == 1

    _add_filing(body, "0000000001-26-000005", URL.replace("ex991", "ex995"),
                eligible_at="2026-11-01T12:00:00Z", body="new document content " * 80)
    changed = publication.sec_bodies_snapshot_identity(
        body, snapshot, correction_table_path=correction_path, store_id=STORE_ID,
    )
    assert changed.identity != first.identity
    assert changed.included_count == 2


def test_sec_document_id_normalizes_accession_scheme_and_host():
    normalized = publication._sec_body_document_id(
        ACCESSION, URL,
    )
    variant = publication._sec_body_document_id(
        ACCESSION.replace("-", ""), URL.replace("https://www.sec.gov", "http://WWW.SEC.GOV"),
    )
    assert variant == normalized


def test_sec_body_dedup_normalizes_accession_scheme_and_host(tmp_path):
    body = _body_store(tmp_path / "news_bodies_v2.sqlite3")
    _add_filing(body, ACCESSION, URL)
    snapshot = _snapshot(tmp_path / "snapshot.sqlite3")
    _add_snapshot_document(
        snapshot, accession=ACCESSION.replace("-", ""),
        url=URL.replace("https://www.sec.gov", "http://WWW.SEC.GOV"),
    )
    summary = publication.sec_bodies_snapshot_identity(
        body, snapshot, correction_table_path=_corrections(tmp_path), store_id=STORE_ID,
    )
    assert summary.included_count == 0
    assert summary.duplicate_count == 1


def test_ineligible_snapshot_match_does_not_deduplicate_sec_body(tmp_path):
    body = _body_store(tmp_path / "news_bodies_v2.sqlite3")
    _add_filing(body, ACCESSION, URL)
    snapshot = _snapshot(tmp_path / "snapshot.sqlite3")
    _add_snapshot_document(
        snapshot, accession=ACCESSION, url=URL, extraction_status="fetch_failed",
    )
    summary = publication.sec_bodies_snapshot_identity(
        body, snapshot, correction_table_path=_corrections(tmp_path), store_id=STORE_ID,
    )
    assert summary.included_count == 1
    assert summary.duplicate_count == 0


def test_sec_body_identity_changes_when_ticker_is_added(tmp_path):
    body = _body_store(tmp_path / "news_bodies_v2.sqlite3")
    version_id = _add_filing(body, ACCESSION, URL)
    snapshot = _snapshot(tmp_path / "snapshot.sqlite3")
    correction_path = _corrections(tmp_path)
    before = publication.sec_bodies_snapshot_identity(
        body, snapshot, correction_table_path=correction_path, store_id=STORE_ID,
    )
    temporal_evidence.register_version(
        body, version_id=version_id, ticker="BBB", kind="filing",
        evidence_id=f"sec:{ACCESSION}", evidence_role="evidence",
        association_observed_at=datetime(2026, 10, 31, tzinfo=timezone.utc),
        eligible_at=datetime(2026, 10, 30, 19, 58, tzinfo=timezone.utc),
        published_at_source="sec_index", published_at_precision="second",
        url=URL, provider="sec", source_class="official_government",
        sec_accession=ACCESSION,
    )
    after = publication.sec_bodies_snapshot_identity(
        body, snapshot, correction_table_path=correction_path, store_id=STORE_ID,
    )
    assert after.identity != before.identity


def test_sec_body_url_aliases_group_once_and_union_tickers(tmp_path):
    body = _body_store(tmp_path / "news_bodies_v2.sqlite3")
    _add_filing(body, ACCESSION, URL, ticker="AAA")
    _add_filing(
        body, ACCESSION, URL.replace("https://www.sec.gov", "http://WWW.SEC.GOV"),
        ticker="BBB",
    )
    snapshot = _snapshot(tmp_path / "snapshot.sqlite3")
    correction_path = _corrections(tmp_path)

    documents = list(publication._iter_sec_body_documents(
        body, snapshot, page_size=10, correction_table_path=correction_path, store_id=STORE_ID,
    ))
    summary = publication.sec_bodies_snapshot_identity(
        body, snapshot, correction_table_path=correction_path, store_id=STORE_ID,
    )

    assert len(documents) == 1
    assert json.loads(documents[0].document["ticker_associations"]) == ["AAA", "BBB"]
    assert summary.included_count == 1


def test_sec_body_url_aliases_with_different_bodies_are_ineligible(tmp_path):
    body = _body_store(tmp_path / "news_bodies_v2.sqlite3")
    _add_filing(body, ACCESSION, URL, ticker="AAA")
    _add_filing(
        body, ACCESSION, URL.replace("https://www.sec.gov", "http://WWW.SEC.GOV"),
        ticker="BBB", body="different filing text " * 140,
    )
    snapshot = _snapshot(tmp_path / "snapshot.sqlite3")
    correction_path = _corrections(tmp_path)

    documents = list(publication._iter_sec_body_documents(
        body, snapshot, page_size=10, correction_table_path=correction_path, store_id=STORE_ID,
    ))
    summary = publication.sec_bodies_snapshot_identity(
        body, snapshot, correction_table_path=correction_path, store_id=STORE_ID,
    )

    assert len(documents) == 1
    assert documents[0].eligibility == "ineligible"
    assert documents[0].ineligibility_reason == "conflicting_document_body"
    assert summary.included_count == 0
    assert summary.excluded_count == 1


def test_sec_body_url_aliases_with_different_acceptance_times_are_ineligible(tmp_path):
    body = _body_store(tmp_path / "news_bodies_v2.sqlite3")
    accession = "0000000001-26-000009"
    _add_filing(body, accession, URL, ticker="AAA")
    _add_filing(
        body, accession, URL.replace("https://www.sec.gov", "http://WWW.SEC.GOV"),
        ticker="BBB", eligible_at="2026-10-30T20:01:00Z",
    )
    snapshot = _snapshot(tmp_path / "snapshot.sqlite3")
    correction_path = _corrections(tmp_path)

    documents = list(publication._iter_sec_body_documents(
        body, snapshot, page_size=10, correction_table_path=correction_path, store_id=STORE_ID,
    ))
    summary = publication.sec_bodies_snapshot_identity(
        body, snapshot, correction_table_path=correction_path, store_id=STORE_ID,
    )

    assert len(documents) == 1
    assert documents[0].eligibility == "ineligible"
    assert documents[0].ineligibility_reason == "conflicting_available_at"
    assert summary.included_count == 0
    assert summary.excluded_count == 1


def test_sec_body_build_indexes_text_and_enforces_as_of_cutoff(tmp_path):
    corpus = sqlite3.connect(tmp_path / "corpus.sqlite3")
    init_db(corpus)
    _seed_current_manifest(corpus)
    body = _body_store(tmp_path / "news_bodies_v2.sqlite3")
    _add_filing(body, ACCESSION, URL)
    _add_filing(body, "0000000001-26-000002", URL.replace("ex991", "ex992"),
                eligible_at=None, body="untrusted quartzhelium filing " * 60)
    snapshot = _snapshot(tmp_path / "snapshot.sqlite3")
    correction_path = _corrections(tmp_path)

    result = publication.build_streaming_corpus_and_lexical_index(
        corpus, certified_snapshot_identity=SNAPSHOT_ID, clock=lambda: NOW,
        sec_bodies_conn=body, sec_bodies_correction_table_path=correction_path,
        sec_bodies_store_id=STORE_ID,
    )
    source_id = publication._sec_body_document_id(ACCESSION, URL)
    chunk = corpus.execute(
        "SELECT available_at FROM corpus_build_chunks WHERE build_id=? AND document_id=? LIMIT 1",
        (result.build_id, source_id),
    ).fetchone()
    header = json.loads(corpus.execute(
        "SELECT header_json FROM corpus_publication_builds WHERE build_id=?", (result.build_id,),
    ).fetchone()[0])

    assert chunk == ("2026-10-30T20:05:00Z",)
    assert result.sec_bodies_included_count == 1
    assert result.sec_bodies_excluded_count == 1
    assert corpus.execute(
        """SELECT eligibility,ineligibility_reason FROM corpus_build_source_documents
           WHERE build_id=? AND document_id=?""",
        (result.build_id, publication._sec_body_document_id(
            "0000000001-26-000002", URL.replace("ex991", "ex992"),
        )),
    ).fetchone() == ("ineligible", "SEC filing 0000000001-26-000002 has missing acceptance time")
    assert header["sec_bodies_snapshot_identity"] == "sha256:" + publication.sec_bodies_snapshot_identity(
        body, corpus, correction_table_path=correction_path, store_id=STORE_ID,
    ).identity
    assert retrieve_lexical(
        corpus, "quartzhelium", ticker="AAA", cutoff="2026-10-30T20:04:59Z",
        requested_manifest_id=result.corpus.manifest_id, top_k=5,
    ).results == ()
    assert retrieve_lexical(
        corpus, "quartzhelium", ticker="AAA", cutoff="2026-10-30T20:05:00Z",
        requested_manifest_id=result.corpus.manifest_id, top_k=5,
    ).results
    second = publication.build_streaming_corpus_and_lexical_index(
        corpus, certified_snapshot_identity=SNAPSHOT_ID, clock=lambda: NOW,
        sec_bodies_conn=body, sec_bodies_correction_table_path=correction_path,
        sec_bodies_store_id=STORE_ID,
    )
    assert second.build_id == result.build_id
    assert second.sec_bodies_included_count == result.sec_bodies_included_count


def test_daily_fts_rebuild_fails_when_sec_body_store_is_missing(tmp_path):
    corpus = tmp_path / "corpus.sqlite3"
    alpaca = tmp_path / "alpaca.sqlite3"
    sqlite3.connect(corpus).close()
    sqlite3.connect(alpaca).close()
    paths = daily.Paths(
        market_db=tmp_path / "market.sqlite3", bodies_db=tmp_path / "missing-news-bodies.sqlite3",
        peer_map={}, snapshot_db=None, jobs_db=tmp_path / "jobs.sqlite3", archive_db=tmp_path / "archive.sqlite3",
        corpus_db=corpus, alpaca_news_db=alpaca, corpus_fts_rebuild_enabled=True,
        corpus_min_free_gb=0,
    )

    result = daily._build_corpus_fts_best_effort(paths, lambda _: None)

    assert result["status"] == "failed"
    assert result["reason_code"] == "missing_input"
    assert "CATALYST_NEWS_BODIES_DB" in result["reason"]
