"""M8-A ingest of sealed general source-selection bodies into canonical.

The sealed ``general-public-fulltext-v1`` manifest is *class* selected: every
document is addressed by public identity (URL, source class, times, publisher,
ticker, hash-bound body) and never by benchmark semantics. This module is the
write path that makes a non-empty selection real:

* body bytes are re-read from the caller-supplied body root (the manifest was
  already re-hashed on load, so an unreadable/short body fails closed here);
* SEC Archives bodies bind to the filing that owns the accession and are
  written as ``filing_documents`` rows with a persisted M3-4 reparse;
* every other document is written as a canonical ``NEWS`` asset with a real
  ``FULL_TEXT`` body (title/description-only rows never mint a body).

Document identity is a function of *public* document identity only:

* filings: ``asset_id`` derives from the existing ``filing_id``, and the
  canonical content version derives from the normalized body hash, so an
  already-known accession reproduces its own corpus document identity;
* news/official documents: ``article_id`` derives from the canonical URL.

No identity from a previous generation is copied, and no benchmark field is
read, accepted, or persisted here.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from catalyst_data.articles.body_recovery import (
    persist_article_content_repair,
    recover_body,
)
from catalyst_data.articles.url_normalize import normalize_url
from catalyst_data.canonical.backfill import (
    MATERIALITY_VERSION,
    NEWS_NORMALIZER_VERSION,
    SEC_NORMALIZER_VERSION,
    _now_utc,
    _state_serving_status,
    _upsert_asset,
    _upsert_subtype_assoc,
)
from catalyst_data.canonical.ids import (
    asset_id,
    canonical_content_version_id,
    canonical_json_bytes,
    sha256_identity,
)
from catalyst_data.canonical.source_selection import (
    SelectedDocument,
    SourceSelectionManifest,
)

INGEST_POLICY_ID = "general-public-fulltext-ingest-v1"

# https://www.sec.gov/Archives/edgar/data/<cik>/<accession-nodash>/<file>
_SEC_ARCHIVES = re.compile(
    r"^https://www\.sec\.gov/Archives/edgar/data/[0-9]+/([0-9]{18})/([^/?#]+)$"
)
_EXHIBIT99 = re.compile(r"(?i)(?:^|[-_])ex[-_]?99")


class SourceIngestError(RuntimeError):
    """A sealed selection document cannot be ingested without inventing data."""


@dataclass(frozen=True)
class IngestedDocument:
    canonical_url: str
    source_class: str
    document_kind: str
    asset_id: str
    canonical_content_version_id: str
    content_state: str
    body_sha256: str
    char_len: int

    def as_dict(self) -> dict[str, object]:
        return {
            "canonical_url": self.canonical_url,
            "source_class": self.source_class,
            "document_kind": self.document_kind,
            "asset_id": self.asset_id,
            "canonical_content_version_id": self.canonical_content_version_id,
            "content_state": self.content_state,
            "body_sha256": self.body_sha256,
            "char_len": self.char_len,
        }


@dataclass(frozen=True)
class SourceIngestResult:
    selection_policy_id: str
    source_selection_id: str
    ingest_policy_id: str
    documents: tuple[IngestedDocument, ...]
    body_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "ingest_policy_id": self.ingest_policy_id,
            "selection_policy_id": self.selection_policy_id,
            "source_selection_id": self.source_selection_id,
            "document_count": len(self.documents),
            "body_bytes": self.body_bytes,
            "documents": [document.as_dict() for document in self.documents],
        }


def _read_body(document: SelectedDocument, body_root: Path) -> bytes:
    raw = (Path(body_root) / document.body_path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != document.body_sha256:
        raise SourceIngestError(
            f"body hash mismatch while ingesting {document.canonical_url}"
        )
    return raw


def _content_hash_for_state(content_state: str, **fields: object) -> str:
    """State-bound content hash (identical to the M3 backfill contract)."""
    payload: dict[str, object] = {"content_state": content_state}
    payload.update(fields)
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _news_article_id(document: SelectedDocument) -> str:
    """Public, URL-derived official-document identity (never generation-bound)."""
    return "official:" + sha256_identity(
        {"source_selection": INGEST_POLICY_ID, "url": document.canonical_url}
    )


def _ensure_content_version(
    conn: sqlite3.Connection,
    *,
    asset_id_value: str,
    content_version_id: str,
    content_hash: str,
    normalizer_version: str,
    payload_ref: str | None,
) -> None:
    """Idempotently materialize the parent asset's content-version row."""
    existing = conn.execute(
        "SELECT content_hash FROM canonical_content_versions "
        "WHERE canonical_content_version_id=?",
        (content_version_id,),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != content_hash:
            raise SourceIngestError(
                f"content version {content_version_id} conflicts on content_hash"
            )
        return
    ordinal = conn.execute(
        "SELECT COALESCE(MAX(version_ordinal), 0) + 1 "
        "FROM canonical_content_versions WHERE asset_id=?",
        (asset_id_value,),
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO canonical_content_versions (
               canonical_content_version_id, asset_id, content_hash,
               normalizer_version, materiality_version, version_ordinal,
               created_at, payload_ref
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            content_version_id,
            asset_id_value,
            content_hash,
            normalizer_version,
            MATERIALITY_VERSION,
            ordinal,
            _now_utc(),
            payload_ref,
        ),
    )


def _sec_url_parts(url: str) -> tuple[str, str] | None:
    match = _SEC_ARCHIVES.match(url)
    if match is None:
        return None
    nodash, filename = match.group(1), match.group(2)
    dashed = f"{nodash[:10]}-{nodash[10:12]}-{nodash[12:]}"
    return dashed, filename


def _derive_document_role(document: SelectedDocument, filename: str) -> str:
    if document.document_role:
        return document.document_role
    if _EXHIBIT99.search(filename):
        return "exhibit_99_1"
    return "primary_doc"


def _ingest_sec_document(
    conn: sqlite3.Connection,
    *,
    document: SelectedDocument,
    raw: bytes,
    now: str,
) -> IngestedDocument:
    from catalyst_data.sec.document_cells import compute_document_id
    from catalyst_data.sec.extract import extract_document_text
    from catalyst_data.sec.reparse import persist_filing_document_reparse, reparse_filing

    parts = _sec_url_parts(document.canonical_url)
    if parts is None:
        raise SourceIngestError(
            "SEC Archives document URL is not in the canonical archived form"
        )
    accession_dashed, filename = parts
    if document.filing_accession and document.filing_accession != accession_dashed:
        raise SourceIngestError(
            "filing_accession does not match the SEC Archives URL accession"
        )
    filing = conn.execute(
        "SELECT * FROM filings WHERE accession_number=?", (accession_dashed,)
    ).fetchone()
    if filing is None:
        raise SourceIngestError(
            f"derivative has no filing for accession {accession_dashed}"
        )
    filing_id = str(filing["filing_id"])
    document_role = _derive_document_role(document, filename)
    document_id = compute_document_id(
        filing_id=filing_id,
        accession_number=accession_dashed,
        document_role=document_role,
        document_file=filename,
        document_url=document.canonical_url,
    )

    if conn.execute(
        "SELECT 1 FROM filing_documents WHERE filing_id=? AND document_id=?",
        (filing_id, document_id),
    ).fetchone() is None:
        conn.execute(
            """INSERT INTO filing_documents (
                   filing_id, document_url, document_type, text, char_len,
                   content_type, byte_size, extraction_status, extracted_at,
                   document_id, parser_version, document_hash, parse_quality,
                   section_parse_degraded
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                filing_id,
                document.canonical_url,
                document_role,
                None,
                None,
                "text/html",
                len(raw),
                "success",
                now,
                document_id,
                None,
                None,
                None,
                None,
            ),
        )
        conn.commit()

    extracted = extract_document_text(
        raw,
        content_type="text/html",
        is_primary=False,
        parser_version=SEC_NORMALIZER_VERSION,
    )
    if extracted.status != "success" or not (extracted.text or "").strip():
        raise SourceIngestError(
            f"SEC document extraction failed ({extracted.status}) for {document_id}"
        )
    parsed = reparse_filing(raw, accession_dashed)
    persist_filing_document_reparse(
        conn,
        filing_id=filing_id,
        document_id=document_id,
        result=parsed,
        extracted_text=extracted.text,
    )

    from catalyst_data.corpus.news_v2 import _normalize_text

    text = _normalize_text(extracted.text)
    if not text:
        raise SourceIngestError(f"SEC document {document_id} normalized to empty text")
    content_hash = _content_hash_for_state("FULL_TEXT", normalized_body=text)
    asset = asset_id(asset_type="FILING", source_table="filings", source_pk=filing_id)
    version_id = canonical_content_version_id(
        asset_id=asset,
        content_hash=content_hash,
        normalizer_version=SEC_NORMALIZER_VERSION,
        materiality_version=MATERIALITY_VERSION,
    )
    _ensure_content_version(
        conn,
        asset_id_value=asset,
        content_version_id=version_id,
        content_hash=content_hash,
        normalizer_version=SEC_NORMALIZER_VERSION,
        payload_ref=filing["raw_asset_id"],
    )
    _upsert_subtype_assoc(
        conn,
        asset_id=asset,
        subtype_table="filing_documents",
        subtype_pk="document_id",
        subtype_pk_value=document_id,
        canonical_content_version_id=version_id,
    )
    conn.commit()
    return IngestedDocument(
        canonical_url=document.canonical_url,
        source_class=document.source_class,
        document_kind="filing_document",
        asset_id=asset,
        canonical_content_version_id=version_id,
        content_state="FULL_TEXT",
        body_sha256=document.body_sha256,
        char_len=len(text),
    )


def _ingest_public_news(
    conn: sqlite3.Connection,
    *,
    document: SelectedDocument,
    raw: bytes,
    now: str,
) -> IngestedDocument:
    if not document.title:
        raise SourceIngestError(
            "a news/official source-selection document requires an explicit "
            f"title (no title is ever invented): {document.canonical_url}"
        )
    if not document.tickers:
        raise SourceIngestError("a news/official document requires a ticker")
    ticker = document.tickers[0]
    article_id = _news_article_id(document)
    # raw_assets v2 contract: asset_id == 'raw:' || request_id.
    request_id = sha256_identity({"article": article_id})
    raw_asset_id = f"raw:{request_id}"
    published = document.source_published_at
    reference_date = published[:10]

    conn.execute(
        """INSERT OR IGNORE INTO raw_assets (
               asset_id, ticker, source_type, reference_date, fetched_at,
               data_version, content_raw, response_sha256, request_id, page_no,
               content_encoding
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            raw_asset_id,
            ticker,
            "source_selection",
            reference_date,
            document.fetched_at,
            "v2",
            raw,
            document.body_sha256,
            request_id,
            1,
            "identity",
        ),
    )
    body_text = raw.decode("utf-8")
    recovery = recover_body(
        {
            "title": document.title,
            "description": None,
            "article_url": document.canonical_url,
        },
        raw_payload={"body": body_text},
    )
    if recovery.content_state != "FULL_TEXT" or not recovery.content_hash:
        raise SourceIngestError(
            f"{document.canonical_url} did not mint FULL_TEXT "
            f"({recovery.content_state})"
        )
    normalized = normalize_url(document.canonical_url)
    normalized_url = None if normalized.unknown else normalized.value
    conn.execute(
        """INSERT OR IGNORE INTO articles (
               article_id, raw_asset_id, provider, source_type, ticker,
               reference_date, published_utc, title, description, article_url,
               publisher_name, source_class, is_canonical, is_rag_eligible,
               created_at, normalized_url
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            article_id,
            raw_asset_id,
            document.provider,
            "source_selection",
            ticker,
            reference_date,
            published,
            document.title,
            None,
            document.canonical_url,
            document.publisher,
            document.source_class,
            1,
            1,
            now,
            normalized_url,
        ),
    )
    conn.execute(
        """INSERT OR IGNORE INTO article_tickers (
               article_id, ticker, raw_asset_id, reference_date, is_canonical
           ) VALUES (?,?,?,?,1)""",
        (article_id, ticker, raw_asset_id, reference_date),
    )
    conn.commit()
    persist_article_content_repair(
        conn,
        article_id=article_id,
        normalized_url=normalized,
        result=recovery,
    )

    asset = asset_id(asset_type="NEWS", source_table="articles", source_pk=article_id)
    version_id = canonical_content_version_id(
        asset_id=asset,
        content_hash=recovery.content_hash,
        normalizer_version=NEWS_NORMALIZER_VERSION,
        materiality_version=MATERIALITY_VERSION,
    )
    serving = _state_serving_status(
        content_state="FULL_TEXT",
        eligible_at=published,
        parse_quality="not_applicable",
    )
    _upsert_asset(
        conn,
        asset_id=asset,
        asset_type="NEWS",
        issuer_id=f"issuer:{ticker}",
        tickers=list(document.tickers),
        provider=document.provider,
        publisher=document.publisher,
        canonical_url=normalized_url or document.canonical_url,
        source_class=document.source_class,
        source_published_at=published,
        eligible_at=published,
        eligible_at_reason="publication_time_provider",
        temporal_precision="publication_time",
        accepted_time_recovered=0,
        fail_closed=0,
        ingested_at=now,
        content_state="FULL_TEXT",
        serving_status=serving,
        title=document.title,
        content_ref=raw_asset_id,
        parse_quality="not_applicable",
        subtype_metadata={
            "publisher": document.publisher,
            "canonical_url": normalized_url or document.canonical_url,
            "article_url": document.canonical_url,
            "body": recovery.body_text,
        },
        content_version_id=version_id,
        content_hash=recovery.content_hash,
        normalizer_version=NEWS_NORMALIZER_VERSION,
        payload_ref=raw_asset_id,
        subtype_assoc=(("articles", "article_id", article_id),),
        provenance=(
            (
                "article",
                article_id,
                recovery.content_hash,
                raw_asset_id,
                NEWS_NORMALIZER_VERSION,
            ),
        ),
    )
    for ticker_value in document.tickers:
        conn.execute(
            """INSERT OR IGNORE INTO canonical_asset_tickers
               (asset_id, ticker, issuer_id) VALUES (?, ?, ?)""",
            (asset, ticker_value, f"issuer:{ticker}"),
        )
    conn.commit()
    return IngestedDocument(
        canonical_url=document.canonical_url,
        source_class=document.source_class,
        document_kind="news",
        asset_id=asset,
        canonical_content_version_id=version_id,
        content_state="FULL_TEXT",
        body_sha256=document.body_sha256,
        char_len=len(recovery.body_text or ""),
    )


def ingest_source_selection_documents(
    conn: sqlite3.Connection,
    *,
    manifest: SourceSelectionManifest,
    body_root: str | Path,
    now: str | None = None,
) -> SourceIngestResult:
    """Ingest every sealed document body into the canonical registry.

    Idempotent: re-running the same sealed selection re-derives the same public
    identities and re-uses the existing rows. An empty selection performs no
    write at all.
    """
    root = Path(body_root)
    timestamp = now or _now_utc()
    ingested: list[IngestedDocument] = []
    total_bytes = 0
    for document in manifest.documents:
        raw = _read_body(document, root)
        total_bytes += len(raw)
        if _sec_url_parts(document.canonical_url):
            ingested.append(
                _ingest_sec_document(conn, document=document, raw=raw, now=timestamp)
            )
        else:
            ingested.append(
                _ingest_public_news(conn, document=document, raw=raw, now=timestamp)
            )
    return SourceIngestResult(
        selection_policy_id=manifest.selection_policy_id,
        source_selection_id=manifest.source_selection_id,
        ingest_policy_id=INGEST_POLICY_ID,
        documents=tuple(ingested),
        body_bytes=total_bytes,
    )


__all__ = [
    "INGEST_POLICY_ID",
    "IngestedDocument",
    "SourceIngestError",
    "SourceIngestResult",
    "ingest_source_selection_documents",
]
