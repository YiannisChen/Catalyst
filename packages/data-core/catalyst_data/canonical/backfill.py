"""V1.1 canonical projection engine + structured-fact certification (M3-2).

Execution-lock §A/§C/§D: idempotent backfill from subtype rows into the
canonical registry. This task proves the schema/identity contract on fixtures;
the final text projection over persisted M3-3/M3-4/M3-5 repair outputs is
completed in M3-5B (``backfill.py`` is the sole production call site then).

Structured facts (``ohlcv``, ``macro_observations``, ``fundamental_statements``)
are certified into ``canonical_structured_fact_refs`` with ``fact_id``; they are
never projected as STRUCTURED_CONTEXT text assets and are never placed in
LanceDB.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from catalyst_data.articles.body_recovery import recover_body
from catalyst_data.canonical.ids import (
    asset_id,
    canonical_content_version_id,
    canonical_json_bytes,
    fact_id,
    sha256_identity,
    source_pk_json,
)
from catalyst_data.corpus.news_v2 import _normalize_text
from catalyst_data.corpus.source_classifier import classify
from catalyst_data.sec.eligible_at import derive_eligible_at
from catalyst_data.trading_calendar import session_close_utc

NEWS_NORMALIZER_VERSION = "news_body_v1"
SEC_NORMALIZER_VERSION = "sec_extract_v1"
MATERIALITY_VERSION = "materiality_v1"

def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _normalize_utc_z(value: object) -> str | None:
    """Normalize an aware timestamp to UTC second-resolution Z, else None.

    Naive datetimes and date-only strings are rejected (execution-lock §D: a
    naive datetime is never treated as UTC for eligibility purposes).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc).replace(microsecond=0).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None or dt.utcoffset() is None:
            return None
        return dt.astimezone(timezone.utc).replace(microsecond=0).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    return None


def _z(value: datetime | None) -> str | None:
    """Format an aware datetime as UTC second-resolution Z, else None."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _parse_accepted_time_utc(value: object) -> datetime | None:
    """Parse a persisted ``accepted_time_utc`` Z string, failing closed.

    None/empty/malformed values parse to None (never an accepted time), so an
    unusable stored accepted time flows through ``derive_eligible_at`` to the
    fail-closed vocabulary instead of being treated as UTC.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt


def _content_hash_for_state(content_state: str, **fields: object) -> str:
    payload: dict[str, object] = {"content_state": content_state}
    payload.update(fields)
    return sha256_identity(payload)


def _state_serving_status(
    *,
    content_state: str,
    eligible_at: str | None,
    parse_quality: str,
) -> str:
    """Execution-lock §H exact serving_status mapping."""
    if eligible_at is None or content_state in ("EMPTY", "FAILED"):
        return "excluded"
    if content_state == "FULL_TEXT" and parse_quality != "failed":
        return "body_candidate"
    if content_state in ("TITLE_ONLY", "METADATA_ONLY"):
        return "lead_candidate"
    return "excluded"


@dataclass(frozen=True)
class BackfillResult:
    assets: int
    content_versions: int
    associations: int
    tickers: int
    fact_refs: int
    excluded_assets: int


class CanonicalBackfillError(RuntimeError):
    """Fail-closed backfill conflict or integrity violation."""


def _provenance_version(asset_id_value: str, content_state: str) -> str:
    return sha256_identity(
        {"asset_id": asset_id_value, "content_state": content_state}
    )


def _record_provenance(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    entity_version: str,
    raw_asset_id: str,
    normalizer_version: str,
    canonical_asset_id_value: str | None,
    canonical_content_version_id_value: str | None,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO normalized_provenance (
               entity_type, entity_id, entity_version, raw_asset_id,
               normalizer_version, created_at, canonical_asset_id,
               canonical_content_version_id
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entity_type,
            entity_id,
            entity_version,
            raw_asset_id,
            normalizer_version,
            _now_utc(),
            canonical_asset_id_value,
            canonical_content_version_id_value,
        ),
    )


# ---------------------------------------------------------------------------
# News projection
# ---------------------------------------------------------------------------


def _project_news(
    conn: sqlite3.Connection, *, require_repairs: bool = False
) -> tuple[int, int, int, int]:
    rows = conn.execute(
        """SELECT a.* FROM articles a ORDER BY a.article_id"""
    ).fetchall()
    ticker_rows = {
        article_id: [r["ticker"] for r in conn.execute(
            "SELECT ticker FROM article_tickers WHERE article_id=? ORDER BY ticker",
            (article_id,),
        )]
        for article_id in (r["article_id"] for r in rows)
    }
    assets = versions = assocs = tickers = 0
    for row in rows:
        article_id = row["article_id"]
        if require_repairs and row["recovered_content_state"] is None:
            raise CanonicalBackfillError(
                f"news row {article_id} lacks recovered_content_state; "
                f"require_repairs=True"
            )
        asset_id_value = asset_id(
            asset_type="NEWS",
            source_table="articles",
            source_pk=article_id,
        )
        tickers_list = ticker_rows.get(article_id) or []
        if not tickers_list and row["ticker"]:
            tickers_list = [row["ticker"]]
        primary_ticker = row["ticker"] or (tickers_list[0] if tickers_list else None)
        if not primary_ticker:
            raise CanonicalBackfillError(
                f"news row {article_id} has no ticker for issuer_id"
            )

        published = _normalize_utc_z(row["published_utc"])
        source_class = row["source_class"] if row["source_class"] else classify(
            source_type=row["source_type"],
            article_url=row["article_url"],
            publisher=row["publisher_name"],
            provider=row["provider"],
        )
        # M3-5B: the persisted repair output (recovered_content_state /
        # recovered_content_hash / normalized_url) is authoritative and wins
        # over any stale pre-repair subtype value.
        repair_state = row["recovered_content_state"]
        if repair_state is not None:
            content_state = repair_state
            if content_state in ("FULL_TEXT", "TITLE_ONLY", "METADATA_ONLY"):
                if not row["recovered_content_hash"]:
                    raise CanonicalBackfillError(
                        f"news row {article_id} has repair content state "
                        f"{content_state} without its state-bound content hash"
                    )
                content_hash = row["recovered_content_hash"]
            else:  # EMPTY/FAILED: mint no content version
                content_hash = None
            canonical_url = row["normalized_url"] or row["article_url"]
        else:
            # Unrepaired: classify exactly like recover_body with no authentic
            # body (raw_payload={}). One classifier, no drift: empty or
            # whitespace description is EMPTY even when a title exists.
            recovery = recover_body(
                {
                    "title": row["title"],
                    "description": row["description"],
                    "article_url": row["article_url"],
                },
                raw_payload={},
            )
            content_state = recovery.content_state
            content_hash = recovery.content_hash
            canonical_url = row["article_url"]

        eligible_at = published
        if eligible_at is None:
            ingested = _normalize_utc_z(row["created_at"]) or _now_utc()
            eligible_at = ingested
            temporal_precision = "ingestion_time"
            eligible_at_reason = "ingestion_time_conservative"
        else:
            temporal_precision = "publication_time"
            eligible_at_reason = "publication_time_provider"

        parse_quality = "not_applicable"
        serving_status = _state_serving_status(
            content_state=content_state,
            eligible_at=eligible_at,
            parse_quality=parse_quality,
        )
        subtype_metadata = {
            "publisher": row["publisher_name"],
            "canonical_url": canonical_url,
            "article_url": row["article_url"],
        }

        content_version_id_value = None
        if content_hash is not None:
            content_version_id_value = canonical_content_version_id(
                asset_id=asset_id_value,
                content_hash=content_hash,
                normalizer_version=NEWS_NORMALIZER_VERSION,
                materiality_version=MATERIALITY_VERSION,
            )

        _upsert_asset(
            conn,
            asset_id=asset_id_value,
            asset_type="NEWS",
            issuer_id=f"issuer:{primary_ticker}",
            tickers=tickers_list,
            provider=row["provider"],
            publisher=row["publisher_name"],
            canonical_url=canonical_url,
            source_class=source_class,
            source_published_at=published,
            eligible_at=eligible_at,
            eligible_at_reason=eligible_at_reason,
            temporal_precision=temporal_precision,
            accepted_time_recovered=0,
            fail_closed=0,
            ingested_at=_normalize_utc_z(row["created_at"]) or _now_utc(),
            content_state=content_state,
            serving_status=serving_status,
            title=row["title"],
            content_ref=row["raw_asset_id"],
            parse_quality=parse_quality,
            subtype_metadata=subtype_metadata,
            content_version_id=content_version_id_value,
            content_hash=content_hash,
            normalizer_version=NEWS_NORMALIZER_VERSION,
            payload_ref=row["raw_asset_id"],
            subtype_assoc=(("articles", "article_id", article_id),),
            provenance=(
                (
                    "article",
                    article_id,
                    content_hash or _provenance_version(asset_id_value, content_state),
                    row["raw_asset_id"],
                    NEWS_NORMALIZER_VERSION,
                ),
            ),
        )
        for ticker in tickers_list:
            conn.execute(
                """INSERT OR IGNORE INTO canonical_asset_tickers
                   (asset_id, ticker, issuer_id) VALUES (?, ?, ?)""",
                (asset_id_value, ticker, f"issuer:{primary_ticker}"),
            )
            tickers += 1
        assets += 1
        if content_version_id_value is not None:
            versions += 1
        assocs += 1
    return assets, versions, assocs, tickers


# ---------------------------------------------------------------------------
# Filing projection (one FILING asset per accession; documents are content
# versions/sections of the parent asset)
# ---------------------------------------------------------------------------


def _project_filings(
    conn: sqlite3.Connection, *, require_repairs: bool = False
) -> tuple[int, int, int, int]:
    filings = conn.execute(
        "SELECT * FROM filings ORDER BY filing_id"
    ).fetchall()
    assets = versions = assocs = tickers = 0
    for filing in filings:
        filing_id = filing["filing_id"]
        if require_repairs and filing["eligible_at_reason"] is None:
            raise CanonicalBackfillError(
                f"filing {filing_id} lacks persisted temporal repair "
                f"(eligible_at_reason IS NULL); require_repairs=True"
            )
        asset_id_value = asset_id(
            asset_type="FILING",
            source_table="filings",
            source_pk=filing_id,
        )
        docs = conn.execute(
            "SELECT * FROM filing_documents WHERE filing_id=? ORDER BY document_url",
            (filing_id,),
        ).fetchall()
        primary = next(
            (d for d in docs if d["document_type"] == "primary_doc"), None
        ) or (docs[0] if docs else None)

        content_state = "EMPTY"
        content_hash: str | None = None
        parse_quality = "not_applicable"
        content_text = ""
        # Batch B B5: normalizer_version is the persisted parser identity when
        # an M3-4 reparse was persisted; otherwise the versioned default.
        primary_normalizer = SEC_NORMALIZER_VERSION
        if primary is not None and primary["parser_version"]:
            primary_normalizer = primary["parser_version"]
        if primary is not None:
            status = primary["extraction_status"]
            text = primary["text"] or ""
            if status == "success" and text.strip():
                content_state = "FULL_TEXT"
                content_text = _normalize_text(text)
                content_hash = _content_hash_for_state(
                    "FULL_TEXT", normalized_body=content_text
                )
                if require_repairs:
                    # Lock B8: a successful primary without persisted M3-4
                    # repair identity fails closed first.
                    if primary["parser_version"] is None:
                        raise CanonicalBackfillError(
                            f"filing_documents row {primary['document_id']} "
                            f"lacks persisted M3-4 repair "
                            f"(parser_version IS NULL); require_repairs=True"
                        )
                    # Copy the persisted M3-4 parse quality instead of forcing
                    # 'full' on every successful non-empty extract (B8). NULL
                    # or unknown persisted quality fails closed; never coerce.
                    if primary["parse_quality"] not in (
                        "full", "degraded", "not_applicable", "failed",
                    ):
                        raise CanonicalBackfillError(
                            f"filing_documents row {primary['document_id']} "
                            f"persisted parse_quality is NULL or unknown "
                            f"({primary['parse_quality']!r}); require_repairs=True"
                        )
                    parse_quality = primary["parse_quality"]
                else:
                    parse_quality = "full"
            elif status in ("success", "empty", "pdf_skipped"):
                content_state = "EMPTY"
                parse_quality = "not_applicable"
            else:  # fetch_failed / timeout
                content_state = "FAILED"
                parse_quality = "failed"

        repair_persisted = any(
            filing[col] is not None
            for col in (
                "accepted_time_utc",
                "eligible_at",
                "eligible_at_reason",
                "temporal_precision",
                "accepted_time_recovered",
                "eligibility_fail_closed",
            )
        )
        if repair_persisted:
            # Copy the persisted M3-3 repair vocabulary; never clobber a
            # persisted fail_closed_no_time_of_day with no_accepted_time, and
            # keep the persisted flags even when eligible_at is NULL.
            eligible_at = _normalize_utc_z(filing["eligible_at"])
            temporal_precision = filing["temporal_precision"]
            eligible_at_reason = filing["eligible_at_reason"]
            fail_closed = int(filing["eligibility_fail_closed"] or 0)
            accepted_time_recovered = int(filing["accepted_time_recovered"] or 0)
        else:
            # No repair persisted: derive fail-closed. A valid date-only
            # filed_at projects fail_closed_no_time_of_day / unknown_time_of_day,
            # never fail_closed_no_accepted_time / unknown.
            derived = derive_eligible_at(
                {"filed_at": filing["filed_at"]},
                _parse_accepted_time_utc(filing["accepted_time_utc"]),
                approve_latest_plausible=False,
            )
            eligible_at = _z(derived.eligible_at)
            temporal_precision = derived.temporal_precision
            eligible_at_reason = derived.eligible_at_reason
            fail_closed = int(derived.fail_closed)
            accepted_time_recovered = int(derived.accepted_time_recovered)

        serving_status = _state_serving_status(
            content_state=content_state,
            eligible_at=eligible_at,
            parse_quality=parse_quality,
        )
        subtype_metadata = {
            "accession": filing["accession_number"],
            "form_type": filing["form_type"],
            "filing_date": filing["filed_at"],
            "accepted_time": filing["accepted_time_utc"],
            "cik": filing["cik"],
        }

        content_version_id_value = None
        if content_hash is not None:
            content_version_id_value = canonical_content_version_id(
                asset_id=asset_id_value,
                content_hash=content_hash,
                normalizer_version=primary_normalizer,
                materiality_version=MATERIALITY_VERSION,
            )

        _upsert_asset(
            conn,
            asset_id=asset_id_value,
            asset_type="FILING",
            issuer_id=f"issuer:{filing['cik']}",
            tickers=[filing["ticker"]] if filing["ticker"] else [],
            provider="sec",
            publisher=None,
            canonical_url=filing["url"],
            source_class="official_government",
            source_published_at=None,
            eligible_at=eligible_at,
            eligible_at_reason=eligible_at_reason,
            temporal_precision=temporal_precision,
            accepted_time_recovered=accepted_time_recovered,
            fail_closed=fail_closed,
            ingested_at=_normalize_utc_z(filing["created_at"]) or _now_utc(),
            content_state=content_state,
            serving_status=serving_status,
            title=None,
            content_ref=filing["primary_document"],
            parse_quality=parse_quality,
            subtype_metadata=subtype_metadata,
            content_version_id=content_version_id_value,
            content_hash=content_hash,
            normalizer_version=primary_normalizer,
            payload_ref=filing["raw_asset_id"],
            subtype_assoc=(("filings", "filing_id", filing_id),),
            provenance=(
                (
                    "filing",
                    filing_id,
                    content_hash or _provenance_version(asset_id_value, content_state),
                    filing["raw_asset_id"],
                    primary_normalizer,
                ),
            ),
        )
        assets += 1
        assocs += 1
        if content_version_id_value is not None:
            versions += 1
        if filing["ticker"]:
            conn.execute(
                """INSERT OR IGNORE INTO canonical_asset_tickers
                   (asset_id, ticker, issuer_id) VALUES (?, ?, ?)""",
                (asset_id_value, filing["ticker"], f"issuer:{filing['cik']}"),
            )
            tickers += 1

        # Filing documents are content-version provenance rows of the parent
        # asset. A successfully parsed document (success + non-empty text)
        # binds the parent primary content version (Batch B B4); EMPTY/FAILED
        # documents bind no content version (nullable). The binding writer
        # updates stale values instead of ignoring them (B7).
        for doc in docs:
            if require_repairs and doc["parser_version"] is None:
                raise CanonicalBackfillError(
                    f"filing_documents row {doc['document_id']} lacks persisted "
                    f"M3-4 repair (parser_version IS NULL); require_repairs=True"
                )
            if not doc["document_id"]:
                if require_repairs:
                    # Lock B8: a NULL document_id row cannot be bound, but in
                    # final mode it is still a repair failure; fail closed.
                    raise CanonicalBackfillError(
                        f"filing_documents row for filing {filing_id} lacks a "
                        f"bindable document_id (NULL); require_repairs=True"
                    )
                # Legacy document row without a stable 64-hex document identity
                # cannot form a canonical_subtype_assoc binding or provenance
                # row (PK/entity_id require a non-null value); skip it.
                continue
            doc_text = doc["text"] or ""
            if require_repairs and doc["document_hash"] and doc_text.strip():
                stored_hash = hashlib.sha256(
                    _normalize_text(doc_text).encode("utf-8")
                ).hexdigest()
                if stored_hash != doc["document_hash"]:
                    raise CanonicalBackfillError(
                        f"filing_documents row {doc['document_id']} persisted "
                        f"document_hash does not match normalized stored text"
                    )
            successfully_parsed = (
                doc["extraction_status"] == "success" and doc_text.strip()
            )
            doc_version_id = content_version_id_value if successfully_parsed else None
            doc_entity_version = (
                content_hash
                if doc_version_id is not None
                else _provenance_version(asset_id_value, content_state)
            )
            _upsert_subtype_assoc(
                conn,
                asset_id=asset_id_value,
                subtype_table="filing_documents",
                subtype_pk="document_id",
                subtype_pk_value=doc["document_id"],
                canonical_content_version_id=doc_version_id,
            )
            _record_provenance(
                conn,
                entity_type="filing",
                entity_id=doc["document_id"],
                entity_version=doc_entity_version,
                raw_asset_id=filing["raw_asset_id"],
                normalizer_version=primary_normalizer,
                canonical_asset_id_value=asset_id_value,
                canonical_content_version_id_value=doc_version_id,
            )
            assocs += 1
    return assets, versions, assocs, tickers


def _upsert_subtype_assoc(
    conn: sqlite3.Connection,
    *,
    asset_id: str,
    subtype_table: str,
    subtype_pk: str,
    subtype_pk_value: str,
    canonical_content_version_id: str | None,
) -> None:
    """Write one canonical_subtype_assoc binding (Batch B B7).

    INSERT on a new row; no-op on an equal binding; UPDATE the binding when
    the same asset's content version changed (including NULL -> non-NULL after
    a new primary parse); fail closed on an existing row owned by a different
    asset. Never leaves a stale ``canonical_content_version_id``.
    """
    existing = conn.execute(
        "SELECT asset_id, canonical_content_version_id FROM canonical_subtype_assoc "
        "WHERE subtype_table=? AND subtype_pk=? AND subtype_pk_value=?",
        (subtype_table, subtype_pk, subtype_pk_value),
    ).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO canonical_subtype_assoc (
                   asset_id, subtype_table, subtype_pk, subtype_pk_value,
                   canonical_content_version_id, created_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                asset_id, subtype_table, subtype_pk, subtype_pk_value,
                canonical_content_version_id, _now_utc(),
            ),
        )
        return
    if existing["asset_id"] != asset_id:
        raise CanonicalBackfillError(
            f"subtype association {subtype_table}/{subtype_pk}/{subtype_pk_value} "
            f"belongs to asset {existing['asset_id']}; cannot rebind to {asset_id}"
        )
    if existing["canonical_content_version_id"] == canonical_content_version_id:
        return  # idempotent no-op
    conn.execute(
        """UPDATE canonical_subtype_assoc SET canonical_content_version_id=?
           WHERE subtype_table=? AND subtype_pk=? AND subtype_pk_value=?""",
        (canonical_content_version_id, subtype_table, subtype_pk, subtype_pk_value),
    )


# ---------------------------------------------------------------------------
# Shared asset writer
# ---------------------------------------------------------------------------


def _upsert_asset(
    conn: sqlite3.Connection,
    *,
    asset_id: str,
    asset_type: str,
    issuer_id: str,
    tickers: list[str],
    provider: str,
    publisher: str | None,
    canonical_url: str | None,
    source_class: str,
    source_published_at: str | None,
    eligible_at: str | None,
    eligible_at_reason: str,
    temporal_precision: str,
    accepted_time_recovered: int,
    fail_closed: int,
    ingested_at: str,
    content_state: str,
    serving_status: str,
    title: str | None,
    content_ref: str | None,
    parse_quality: str,
    subtype_metadata: dict[str, object],
    content_version_id: str | None,
    content_hash: str | None,
    normalizer_version: str,
    payload_ref: str | None,
    subtype_assoc: tuple[tuple[str, str, str], ...],
    provenance: tuple[tuple[str, str, str, str, str], ...],
) -> None:
    existing = conn.execute(
        "SELECT * FROM canonical_assets WHERE asset_id=?", (asset_id,)
    ).fetchone()
    if existing is not None:
        for field in (
            "asset_type", "issuer_id", "provider", "publisher", "canonical_url",
            "source_class", "source_published_at", "eligible_at",
            "temporal_precision", "content_state", "serving_status", "title",
            "content_ref", "parse_quality",
        ):
            if existing[field] != locals().get(field):
                raise CanonicalBackfillError(
                    f"canonical asset {asset_id} projected with conflicting {field}"
                )
    else:
        conn.execute(
            """INSERT INTO canonical_assets (
                   asset_id, asset_type, issuer_id, tickers_json, provider,
                   publisher, canonical_url, source_class, source_published_at,
                   eligible_at, eligible_at_reason, temporal_precision,
                   accepted_time_recovered, fail_closed, ingested_at,
                   content_state, serving_status, title, content_ref,
                   dedup_cluster_id, independence_group_id, parse_quality,
                   subtype_metadata, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         NULL, NULL, ?, ?, ?, ?)""",
            (
                asset_id, asset_type, issuer_id,
                json.dumps(sorted(tickers), ensure_ascii=False, sort_keys=True),
                provider, publisher, canonical_url, source_class,
                source_published_at, eligible_at, eligible_at_reason,
                temporal_precision, accepted_time_recovered, fail_closed,
                ingested_at, content_state, serving_status, title, content_ref,
                parse_quality,
                json.dumps(subtype_metadata, ensure_ascii=False, sort_keys=True),
                _now_utc(), _now_utc(),
            ),
        )

    if content_version_id is not None and content_hash is not None:
        existing_version = conn.execute(
            "SELECT * FROM canonical_content_versions "
            "WHERE canonical_content_version_id=?",
            (content_version_id,),
        ).fetchone()
        if existing_version is not None:
            if existing_version["content_hash"] != content_hash:
                raise CanonicalBackfillError(
                    f"content version {content_version_id} conflicts on content_hash"
                )
        else:
            ordinal = conn.execute(
                "SELECT COALESCE(MAX(version_ordinal), 0) + 1 "
                "FROM canonical_content_versions WHERE asset_id=?",
                (asset_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO canonical_content_versions (
                       canonical_content_version_id, asset_id, content_hash,
                       normalizer_version, materiality_version, version_ordinal,
                       created_at, payload_ref
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    content_version_id, asset_id, content_hash,
                    normalizer_version, MATERIALITY_VERSION, ordinal,
                    _now_utc(), payload_ref,
                ),
            )

    for subtype_table, subtype_pk, subtype_pk_value in subtype_assoc:
        _upsert_subtype_assoc(
            conn,
            asset_id=asset_id,
            subtype_table=subtype_table,
            subtype_pk=subtype_pk,
            subtype_pk_value=subtype_pk_value,
            canonical_content_version_id=content_version_id,
        )
    for (
        entity_type, entity_id, entity_version, raw_asset_id, normalizer_version
    ) in provenance:
        _record_provenance(
            conn,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_version=entity_version,
            raw_asset_id=raw_asset_id,
            normalizer_version=normalizer_version,
            canonical_asset_id_value=asset_id,
            canonical_content_version_id_value=content_version_id,
        )


# ---------------------------------------------------------------------------
# Structured-fact certification (never text assets, never LanceDB)
# ---------------------------------------------------------------------------


def _source_row_sha256(
    source_table: str, natural_key: dict[str, object], row: dict[str, object]
) -> str:
    return sha256_identity(
        {
            "source_table": source_table,
            "source_pk_json": source_pk_json(natural_key),
            "row": dict(row),
        }
    )


def _existing_ref_for_natural_key(
    conn: sqlite3.Connection,
    source_table: str,
    natural_key: dict[str, object],
) -> object | None:
    """Find an existing fact ref with the same semantic natural key.

    Rejects non-canonical alternate JSON spellings of the same natural key and
    fact_id/source_pk_json mismatches (execution-lock §A.5 writer tests).
    """
    canonical_pk = source_pk_json(natural_key)
    rows = conn.execute(
        "SELECT * FROM canonical_structured_fact_refs WHERE source_table=?",
        (source_table,),
    ).fetchall()
    for ref in rows:
        try:
            semantic = json.loads(ref["source_pk_json"])
        except (ValueError, TypeError) as exc:
            raise CanonicalBackfillError(
                f"fact ref {ref['fact_id']} has unparsable source_pk_json"
            ) from exc
        if semantic == natural_key:
            if ref["source_pk_json"] != canonical_pk:
                raise CanonicalBackfillError(
                    f"fact ref {ref['fact_id']} uses a noncanonical "
                    f"source_pk_json spelling for natural key {natural_key}"
                )
            return ref
    return None


def _certify_ohlcv(conn: sqlite3.Connection) -> int:
    rows = conn.execute("SELECT * FROM ohlcv ORDER BY symbol, date").fetchall()
    count = 0
    for row in rows:
        natural_key = {"symbol": row["symbol"], "date": row["date"]}
        pk = source_pk_json(natural_key)
        existing = _existing_ref_for_natural_key(conn, "ohlcv", natural_key)
        if existing is not None:
            expected_fact_id = fact_id(
                fact_type="ohlcv", source_table="ohlcv", natural_key=natural_key
            )
            if existing["fact_id"] != expected_fact_id:
                raise CanonicalBackfillError("fact_id/source_pk_json mismatch")
            continue
        if not row["date"]:
            status = "excluded_missing_time"
            eligible_at = None
            precision = "unknown"
        else:
            try:
                eligible_at = session_close_utc(row["date"])
                status = "certified"
                precision = "session_close"
            except ValueError:
                eligible_at = None
                status = "excluded_invalid"
                precision = "unknown"
        row_dict = {
            col: row[col]
            for col in ("symbol", "date", "open", "high", "low", "close", "volume", "source")
        }
        conn.execute(
            """INSERT INTO canonical_structured_fact_refs (
                   fact_id, fact_type, source_table, source_pk_json, eligible_at,
                   temporal_precision, source_row_sha256, certification_status,
                   created_at
               ) VALUES (?, 'ohlcv', 'ohlcv', ?, ?, ?, ?, ?, ?)""",
            (
                fact_id(
                    fact_type="ohlcv", source_table="ohlcv", natural_key=natural_key
                ),
                pk, eligible_at, precision,
                _source_row_sha256("ohlcv", natural_key, row_dict),
                status, _now_utc(),
            ),
        )
        count += 1
    return count


def _certify_macro(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT * FROM macro_observations ORDER BY series_id, observation_date"
    ).fetchall()
    count = 0
    for row in rows:
        natural_key = {
            "series_id": row["series_id"],
            "observation_date": row["observation_date"],
        }
        pk = source_pk_json(natural_key)
        existing = _existing_ref_for_natural_key(
            conn, "macro_observations", natural_key
        )
        if existing is not None:
            expected_fact_id = fact_id(
                fact_type="macro_observation",
                source_table="macro_observations",
                natural_key=natural_key,
            )
            if existing["fact_id"] != expected_fact_id:
                raise CanonicalBackfillError("fact_id/source_pk_json mismatch")
            continue
        eligible_at = _normalize_utc_z(row["released_at"])
        if row["released_at"] is None or row["released_at"] == "":
            status = "excluded_missing_time"
            precision = "unknown"
        elif eligible_at is None:
            status = "excluded_invalid"
            precision = "unknown"
        else:
            status = "certified"
            precision = "release_time"
        row_dict = {
            col: row[col]
            for col in (
                "series_id", "observation_date", "value", "released_at",
                "fetched_at", "raw_asset_id",
            )
        }
        conn.execute(
            """INSERT INTO canonical_structured_fact_refs (
                   fact_id, fact_type, source_table, source_pk_json, eligible_at,
                   temporal_precision, source_row_sha256, certification_status,
                   created_at
               ) VALUES (?, 'macro_observation', 'macro_observations', ?, ?, ?, ?, ?, ?)""",
            (
                fact_id(
                    fact_type="macro_observation",
                    source_table="macro_observations",
                    natural_key=natural_key,
                ),
                pk, eligible_at, precision,
                _source_row_sha256("macro_observations", natural_key, row_dict),
                status, _now_utc(),
            ),
        )
        count += 1
    return count


def _certify_fundamental(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT * FROM fundamental_statements ORDER BY statement_id"
    ).fetchall()
    count = 0
    for row in rows:
        natural_key = {"statement_id": row["statement_id"]}
        pk = source_pk_json(natural_key)
        existing = _existing_ref_for_natural_key(
            conn, "fundamental_statements", natural_key
        )
        if existing is not None:
            expected_fact_id = fact_id(
                fact_type="fundamental_statement",
                source_table="fundamental_statements",
                natural_key=natural_key,
            )
            if existing["fact_id"] != expected_fact_id:
                raise CanonicalBackfillError("fact_id/source_pk_json mismatch")
            continue
        eligible_at = _normalize_utc_z(row["available_at"])
        if row["available_at"] is None or row["available_at"] == "":
            status = "excluded_missing_time"
            precision = "unknown"
        elif eligible_at is None:
            status = "excluded_invalid"
            precision = "unknown"
        else:
            status = "certified"
            precision = "available_time"
        row_dict = {
            col: row[col]
            for col in (
                "statement_id", "raw_asset_id", "provider", "ticker",
                "statement_type", "fiscal_date", "fiscal_period",
                "reported_currency", "available_at", "payload_json", "created_at",
            )
        }
        conn.execute(
            """INSERT INTO canonical_structured_fact_refs (
                   fact_id, fact_type, source_table, source_pk_json, eligible_at,
                   temporal_precision, source_row_sha256, certification_status,
                   created_at
               ) VALUES (?, 'fundamental_statement', 'fundamental_statements', ?, ?, ?, ?, ?, ?)""",
            (
                fact_id(
                    fact_type="fundamental_statement",
                    source_table="fundamental_statements",
                    natural_key=natural_key,
                ),
                pk, eligible_at, precision,
                _source_row_sha256("fundamental_statements", natural_key, row_dict),
                status, _now_utc(),
            ),
        )
        count += 1
    return count


def certify_structured_facts(conn: sqlite3.Connection) -> int:
    """Certify all structured-fact source rows into canonical_structured_fact_refs."""
    return (
        _certify_ohlcv(conn)
        + _certify_macro(conn)
        + _certify_fundamental(conn)
    )


def backfill_from_subtypes(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    require_repairs: bool = False,
) -> BackfillResult:
    """Idempotently project subtype rows into the canonical registry.

    With ``dry_run=True`` nothing is written; the returned result still reports
    the rows that would be created.
    """
    if dry_run:
        # Compute-only path: reuse the writers against a rolled-back shadow by
        # counting planned rows without committing. The writers are idempotent,
        # so we can run them inside a savepoint and roll back.
        conn.execute("SAVEPOINT m3_backfill_dry_run")
        try:
            assets, versions, assocs, tickers = _project_news(
                conn, require_repairs=require_repairs
            )
            f_assets, f_versions, f_assocs, f_tickers = _project_filings(
                conn, require_repairs=require_repairs
            )
            facts = certify_structured_facts(conn)
            result = BackfillResult(
                assets=assets + f_assets,
                content_versions=versions + f_versions,
                associations=assocs + f_assocs,
                tickers=tickers + f_tickers,
                fact_refs=facts,
                excluded_assets=0,
            )
        finally:
            conn.execute("ROLLBACK TO SAVEPOINT m3_backfill_dry_run")
            conn.execute("RELEASE SAVEPOINT m3_backfill_dry_run")
        return result

    conn.execute("BEGIN IMMEDIATE")
    try:
        assets, versions, assocs, tickers = _project_news(
            conn, require_repairs=require_repairs
        )
        f_assets, f_versions, f_assocs, f_tickers = _project_filings(
            conn, require_repairs=require_repairs
        )
        facts = certify_structured_facts(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return BackfillResult(
        assets=assets + f_assets,
        content_versions=versions + f_versions,
        associations=assocs + f_assocs,
        tickers=tickers + f_tickers,
        fact_refs=facts,
        excluded_assets=0,
    )


__all__ = [
    "BackfillResult",
    "CanonicalBackfillError",
    "NEWS_NORMALIZER_VERSION",
    "SEC_NORMALIZER_VERSION",
    "MATERIALITY_VERSION",
    "backfill_from_subtypes",
    "certify_structured_facts",
]
