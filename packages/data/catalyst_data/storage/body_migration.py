"""Copy current Alpaca bodies and audit missing history without fabricating it.

Pure connection helpers: no file opens, network, commits, or legacy inference.
CLI callers must verify immutable source snapshots before supplying connections.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from catalyst_data.dedup.links import normalize_identity_url
from catalyst_data.ingestion import ticker_identity
from catalyst_data.ingestion.time import timefields
from catalyst_data.retrieval import evidence_fts_projection
from catalyst_data.storage import body_versions as V
from catalyst_data.storage import temporal_evidence


def _time(value):
    if value in (None, ''):
        return None
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('migration timestamp has no time zone')
    return parsed.astimezone(timezone.utc)


def _stored_revision(value):
    # Alpaca intake stores updated_at to whole seconds, while its old metadata
    # table retains raw provider precision. Do not mistake a format difference
    # for a different historical body. Same-second collisions stay ambiguous.
    parsed = _time(value)
    return parsed.replace(microsecond=0) if parsed else None


def _body_length(body):
    # Match Alpaca's historical content_chars definition, not raw HTML length.
    body = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', body or '', flags=re.S | re.I)
    return len(' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', body)).split()))


def _articles(source):
    cursor = source.execute(
        'SELECT id,created_at,updated_at,headline,content,first_seen_at,url,source,symbols_json '
        'FROM news ORDER BY id'
    )
    names = [field[0] for field in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def audit_alpaca(source: sqlite3.Connection) -> dict:
    articles = {row['id']: row for row in _articles(source)}
    versions = source.execute('SELECT id,updated_at,content_chars FROM news_versions').fetchall()
    per_article = {}
    current_revisions = {ident: _stored_revision(row['updated_at']) for ident, row in articles.items()}
    matching_current = {}
    missing = lower_bound = ambiguous = orphan = unknown_length = current_without_body = 0
    for ident, updated, length in versions:
        per_article[ident] = per_article.get(ident, 0) + 1
        row = articles.get(ident)
        if row is None:
            orphan += 1
            missing += 1
            continue
        current = current_revisions[ident]
        historical = _stored_revision(updated)
        if current is not None and historical == current:
            matching_current[ident] = matching_current.get(ident, 0) + 1
            current_without_body += not bool((row['content'] or '').strip())
            continue
        missing += 1
        if length is None:
            unknown_length += 1
        elif (row['content'] or '').strip() and int(length) != _body_length(row['content']):
            lower_bound += 1
    ambiguous = sum(max(0, n - 1) for n in matching_current.values())
    return {
        'current_articles': len(articles),
        'nonempty_bodies': sum(bool((row['content'] or '').strip()) for row in articles.values()),
        'current_articles_without_body': sum(not bool((row['content'] or '').strip()) for row in articles.values()),
        'current_metadata_without_body': current_without_body,
        'articles_with_multiple_updates': sum(n > 1 for ident, n in per_article.items() if ident in articles),
        'historical_metadata_without_body': missing,
        'length_different_missing_body_records_lower_bound': lower_bound,
        'unknown_provider_updated_at': sum(not row['updated_at'] for row in articles.values()),
        'same_second_revision_ambiguities': ambiguous,
        'orphan_metadata_records': orphan,
        'historical_records_without_length': unknown_length,
        'exact_distinct_lost_bodies': None,
        'backup_recovery_checked': False,
        'limitations': ['updated_at changes do not prove body changes',
                       'length-different records are not a count of distinct lost body hashes',
                       'missing history is assessed only in this snapshot; backups are not checked',
                       'same-second provider revisions cannot be associated uniquely with the current body'],
    }


@contextmanager
def _batch(destination):
    if not destination.in_transaction:
        destination.execute('BEGIN')
    destination.execute('SAVEPOINT body_migration_batch')
    try:
        yield
    except Exception:
        destination.execute('ROLLBACK TO body_migration_batch')
        destination.execute('RELEASE body_migration_batch')
        raise
    else:
        destination.execute('RELEASE body_migration_batch')


def migrate_alpaca(source, destination, *, observed_at: datetime) -> dict:
    """Copy latest full bodies; the snapshot observation never becomes publication.

    Prior article first_seen is preserved separately. The current body's exact
    historical observation cannot be proven by timestamp/length-only metadata,
    so migration uses the supplied snapshot capture time for its observed_at.
    Archives never acquire legacy status merely by being imported.
    """
    if source is destination:
        raise ValueError('source and destination must be separate connections')
    source_files = {row[2] for row in source.execute('PRAGMA database_list') if row[2]}
    destination_files = {row[2] for row in destination.execute('PRAGMA database_list') if row[2]}
    if source_files & destination_files:
        raise ValueError('source and destination must not be the same database')
    stats = audit_alpaca(source)
    inserted = existing = 0
    with _batch(destination):
        V.ensure_schema(destination)
        temporal_evidence.ensure_schema(destination)
        evidence_fts_projection.ensure_schema(destination)
        for row in _articles(source):
            if not (row['content'] or '').strip():
                continue
            before = destination.total_changes
            version_id = V.put_version(destination, source_id='alpaca_news', document_id=str(row['id']),
                title=row['headline'], body=row['content'], observed_at=observed_at,
                first_seen_at=_time(row['first_seen_at']), published_at=_time(row['created_at']),
                provider_updated_at=_time(row['updated_at']))
            added = int(destination.total_changes > before)
            inserted += added
            existing += not added
            canonical_url = normalize_identity_url(row.get('url'))
            evidence_id = (
                'web:' + hashlib.sha256(canonical_url.encode('utf-8')).hexdigest()[:32]
                if canonical_url else f"alpaca:article:{row['id']}"
            )
            publisher = (row.get('source') or '').strip()
            is_benzinga = publisher.casefold() == 'benzinga'
            origin = 'publisher:benzinga.com' if is_benzinga else (
                f'publisher_name:{publisher.casefold()}' if publisher else None
            )
            published = timefields.external(
                row.get('created_at'), source='alpaca', field='created_at'
            )
            version = destination.execute(
                'SELECT observed_at,published_at,provider_updated_at FROM body_versions WHERE version_id=?',
                (version_id,),
            ).fetchone()
            symbols = json.loads(row.get('symbols_json') or '[]')
            for symbol in symbols:
                try:
                    ticker = ticker_identity.normalize_ticker(symbol)
                except (TypeError, ValueError):
                    continue
                existing_map = destination.execute(
                    'SELECT evidence_id,canonical_url,provider FROM temporal_evidence_map '
                    "WHERE version_id=? AND ticker=? AND kind='body'",
                    (version_id, ticker),
                ).fetchone()
                if existing_map is not None:
                    # put_version already verifies the exact version content.
                    # Preserve its original intake/association metadata: a
                    # later copy does not turn a live observation into backfill.
                    if tuple(existing_map) != (evidence_id, canonical_url, 'alpaca'):
                        raise ValueError('existing temporal mapping conflicts with migrated article identity')
                    continue
                temporal_evidence.register_version(
                    destination, version_id=version_id, ticker=ticker, kind='body',
                    evidence_id=evidence_id,
                    evidence_role='evidence' if is_benzinga else 'lead',
                    association_observed_at=observed_at,
                    eligible_at=_time(row.get('created_at')),
                    published_at_source='api',
                    published_at_precision=published.precision if version[1] else 'unknown',
                    api_published=row.get('created_at'), url=row.get('url'),
                    canonical_url=canonical_url, publisher=publisher or None,
                    publisher_family='benzinga.com' if is_benzinga else None,
                    provider='alpaca', source_class='wire' if is_benzinga else 'news',
                    origin=origin, is_backfill=True, time_flags=list(published.flags),
                )
            evidence_fts_projection.project_evidence_text(destination, [
                evidence_fts_projection.EvidenceText(
                    evidence_id=evidence_id, version_id=version_id,
                    source_id='alpaca_news', document_id=str(row['id']), provider='alpaca',
                    kind='body', evidence_class='body_evidence', title=row['headline'] or '',
                    text=row['content'], origin=origin, provenance='versioned',
                    observed_at=version[0], published_at=version[1],
                    provider_updated_at=version[2],
                )
            ])
    return {**stats, 'inserted_versions': inserted, 'existing_versions': existing,
            'provenance': 'versioned', 'historical_bodies_reconstructed': 0}


def inventory_legacy_chunks(source) -> dict:
    """Count legacy candidates, never reconstruct full original text from chunks.

    Candidate counts do not apply provenance. An explicit cohort and snapshot
    identity must be reviewed before a later legacy catalog/import is created.
    """
    assets, chunks = source.execute('SELECT COUNT(DISTINCT asset_id), COUNT(*) FROM web_body_chunks').fetchone()
    return {'assets': assets, 'chunks': chunks, 'exact_distinct_lost_bodies': None,
            'classification_applied': False, 'provider_modification_history_available': False,
            'limitations': ['chunking normalized original whitespace; full original body is not reconstructed',
                           'discarded later-body changes were not recorded with full version history']}
