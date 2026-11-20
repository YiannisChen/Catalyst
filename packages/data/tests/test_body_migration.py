"""Copy-only migration on synthetic stores, without guessing missing old bodies."""
import sqlite3
from datetime import datetime, timezone

import pytest
from catalyst_data.ingestion.collectors import alpaca
from catalyst_data.storage import body_migration as M
from catalyst_data.storage import body_versions as V
from catalyst_data.storage import temporal_evidence

T = datetime(2026, 10, 23, 19, tzinfo=timezone.utc)


@pytest.fixture
def source():
    conn = sqlite3.connect(':memory:')
    conn.executescript(alpaca.SCHEMA)
    alpaca.store_news(conn, [{'id': 1, 'created_at': '2026-10-20T10:00:00Z',
        'updated_at': '2026-10-20T10:01:00Z', 'headline': 'Title', 'content': '<p>old</p>',
        'url': 'https://benzinga.com/news/1', 'source': 'benzinga', 'symbols': ['AAPL']}],
        '2026-10-20T11:00:00Z')
    alpaca.store_news(conn, [{'id': 1, 'created_at': '2026-10-20T10:00:00Z',
        'updated_at': '2026-10-21T10:00:00Z', 'headline': 'Title', 'content': '<p>new longer</p>',
        'url': 'https://benzinga.com/news/1', 'source': 'benzinga', 'symbols': ['AAPL']}],
        '2026-10-21T11:00:00Z')
    conn.commit()
    yield conn
    conn.close()


def test_audit_separates_missing_metadata_bodies_from_known_changes(source):
    result = M.audit_alpaca(source)
    assert result['current_articles'] == result['nonempty_bodies'] == 1
    assert result['articles_with_multiple_updates'] == 1
    assert result['historical_metadata_without_body'] == 1
    assert result['length_different_missing_body_records_lower_bound'] == 1
    assert result['exact_distinct_lost_bodies'] is None
    assert result['backup_recovery_checked'] is False


def test_same_length_update_is_not_proof_of_different_body_loss(source):
    source.execute('UPDATE news_versions SET content_chars=10')
    result = M.audit_alpaca(source)
    assert result['historical_metadata_without_body'] == 1
    assert result['length_different_missing_body_records_lower_bound'] == 0
    assert result['exact_distinct_lost_bodies'] is None


def test_copy_migration_preserves_current_body_and_conservative_observation(source):
    target = sqlite3.connect(':memory:')
    before = source.total_changes
    result = M.migrate_alpaca(source, target, observed_at=T)
    row, = V.list_versions(target, source_id='alpaca_news', document_id='1')
    assert row['body'] == '<p>new longer</p>'
    assert row['observed_at'] == T.isoformat(timespec='microseconds')
    assert row['first_seen_at'] == '2026-10-20T11:00:00.000000+00:00'
    assert row['provenance'] == 'versioned'
    assert row['legacy_cohort'] is None
    assert result['inserted_versions'] == 1
    temporal = target.execute(
        "SELECT ticker,eligible_at,provider,origin,is_backfill FROM temporal_evidence_map"
    ).fetchall()
    assert temporal == [("AAPL", "2026-10-20T10:00:00.000000+00:00", "alpaca", "publisher:benzinga.com", 1)]
    assert target.execute("SELECT COUNT(*) FROM evidence_text_projection").fetchone()[0] == 1
    assert source.total_changes == before
    assert target.in_transaction
    target.rollback()
    assert not target.execute("SELECT name FROM sqlite_master WHERE name='body_versions'").fetchall()


def test_migration_is_idempotent_and_does_not_reconstruct_old_body(source):
    target = sqlite3.connect(':memory:')
    M.migrate_alpaca(source, target, observed_at=T)
    again = M.migrate_alpaca(source, target, observed_at=T)
    assert again['inserted_versions'] == 0 and again['existing_versions'] == 1
    assert target.execute('SELECT COUNT(*) FROM body_versions').fetchone()[0] == 1
    assert target.execute('SELECT COUNT(*) FROM temporal_evidence_map').fetchone()[0] == 1
    assert target.execute('SELECT COUNT(*) FROM evidence_text_projection').fetchone()[0] == 1


def test_migration_preserves_existing_live_poll_metadata(source):
    target = sqlite3.connect(':memory:')
    target.executescript(alpaca.SCHEMA)
    alpaca.store_news(target, [{'id': 1, 'created_at': '2026-10-20T10:00:00Z',
        'updated_at': '2026-10-21T10:00:00Z', 'headline': 'Title',
        'content': '<p>new longer</p>', 'url': 'https://benzinga.com/news/1',
        'source': 'benzinga', 'symbols': ['AAPL']}], '2026-10-20T11:00:00Z')
    before = target.execute('SELECT * FROM temporal_evidence_map').fetchall()
    versions_before = target.execute('SELECT * FROM temporal_evidence_versions').fetchall()
    assert before and target.execute('SELECT is_backfill FROM temporal_evidence_map').fetchone() == (0,)
    result = M.migrate_alpaca(source, target, observed_at=T)
    assert result['inserted_versions'] == 0
    assert result['existing_versions'] == 1
    assert target.execute('SELECT * FROM temporal_evidence_map').fetchall() == before
    assert target.execute('SELECT * FROM temporal_evidence_versions').fetchall() == versions_before
    assert target.execute('SELECT COUNT(*) FROM evidence_text_projection').fetchone() == (1,)


def test_migration_rejects_existing_map_for_a_different_article_identity(source):
    target = sqlite3.connect(':memory:')
    M.migrate_alpaca(source, target, observed_at=T)
    # Simulate a malformed imported store rather than weakening production guards.
    target.execute('DROP TRIGGER temporal_evidence_map_no_update')
    target.execute("UPDATE temporal_evidence_map SET canonical_url='https://example.com/other'")
    with pytest.raises(ValueError, match='article identity'):
        M.migrate_alpaca(source, target, observed_at=T)


def test_migrated_version_is_visible_through_as_of_and_fts(source):
    target = sqlite3.connect(':memory:')
    M.migrate_alpaca(source, target, observed_at=T)
    result = temporal_evidence.as_of(
        target, "AAPL", datetime(2026, 10, 20, 9, tzinfo=timezone.utc),
        datetime(2026, 10, 23, 20, tzinfo=timezone.utc),
        None, mode="published",
        data_snapshot_id="snapshot-a", correction_table_id="corrections-a",
    )
    assert [row["text"] for row in result["evidence"]] == ["<p>new longer</p>"]
    assert target.execute(
        "SELECT evidence_id FROM evidence_text_fts WHERE evidence_text_fts MATCH 'longer'"
    ).fetchone()[0].startswith("web:")


def test_error_rolls_back_entire_batch_and_preserves_callers_work(source):
    target = sqlite3.connect(':memory:')
    target.execute('CREATE TABLE caller_work (value TEXT)')
    target.execute("INSERT INTO caller_work VALUES ('keep')")
    source.execute("INSERT INTO news SELECT 2,created_at,'no-zone',headline,summary,content,url,author,source,symbols_json,first_seen_at FROM news")
    with pytest.raises(ValueError):
        M.migrate_alpaca(source, target, observed_at=T)
    assert target.execute('SELECT value FROM caller_work').fetchone()[0] == 'keep'
    assert not target.execute("SELECT name FROM sqlite_master WHERE name='body_versions'").fetchall()


def test_timestamp_normalization_does_not_mislabel_current_metadata_as_lost(source):
    source.execute("UPDATE news_versions SET updated_at='2026-10-21T10:00:00+00:00' WHERE updated_at='2026-10-21T10:00:00Z'")
    assert M.audit_alpaca(source)['historical_metadata_without_body'] == 1


def test_empty_body_and_unknown_modification_are_reported_not_invented(source):
    source.execute("UPDATE news SET content='',updated_at=NULL")
    result = M.audit_alpaca(source)
    assert result['nonempty_bodies'] == 0
    assert result['unknown_provider_updated_at'] == 1
    target=sqlite3.connect(':memory:')
    assert M.migrate_alpaca(source,target,observed_at=T)['inserted_versions'] == 0


def test_legacy_inventory_counts_assets_and_retains_unknown_loss_boundary():
    source=sqlite3.connect(':memory:')
    source.execute('CREATE TABLE web_body_chunks(asset_id TEXT, ordinal INTEGER, eligible_at TEXT, fetched_at TEXT)')
    source.executemany('INSERT INTO web_body_chunks VALUES (?,?,?,?)', [('a',0,'old','seen'),('a',1,'old','seen'),('b',0,'old','seen')])
    result=M.inventory_legacy_chunks(source)
    assert result['assets'] == 2 and result['chunks'] == 3
    assert result['exact_distinct_lost_bodies'] is None
    assert result['classification_applied'] is False


def test_current_empty_body_is_not_mislabeled_as_missing_historical_version(source):
    source.execute("UPDATE news SET content=''")
    result = M.audit_alpaca(source)
    assert result['current_articles_without_body'] == 1
    assert result['current_metadata_without_body'] == 1
    assert result['historical_metadata_without_body'] == 1
    assert result['length_different_missing_body_records_lower_bound'] == 0
