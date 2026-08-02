"""Offline SEC materialization helpers (injected transport; no live network)."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from catalyst_data.sec.extract import extract_document_text
from catalyst_data.sec.index_parser import filing_index_urls, parse_filing_index_html
from catalyst_data.manifests.universe import sha256_identity

FetchFn = Callable[[str], tuple[int, bytes, str | None]]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class MaterializeResult:
    status: str
    document_id: str | None = None
    raw_asset_id: str | None = None
    logical_fetch_id: str | None = None
    request_count: int = 0
    error: str | None = None


def logical_fetch_id(run_id: str, cell_id: str) -> str:
    from catalyst_data.ingestion.request_ledger import compute_logical_fetch_id

    return compute_logical_fetch_id(run_id, cell_id)


class FilingDocumentConflictError(ValueError):
    """Conflicting identity or content for an existing filing_documents row."""


def _insert_filing_document_identity_aware(
    conn: sqlite3.Connection,
    *,
    filing_id: str,
    document_url: str,
    document_type: str,
    text: str,
    content_type: str | None,
    byte_size: int,
    document_id: str,
) -> None:
    """Identity-aware INSERT — never INSERT OR REPLACE (delete+insert bypasses immutability)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(filing_documents)").fetchall()}
    has_doc_id = "document_id" in cols

    if has_doc_id:
        by_doc = conn.execute(
            "SELECT filing_id, document_url, document_type, text, document_id "
            "FROM filing_documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        if by_doc is not None:
            if (
                by_doc[0] == filing_id
                and by_doc[1] == document_url
                and by_doc[2] == document_type
                and by_doc[3] == text
                and by_doc[4] == document_id
            ):
                return  # identical — idempotent
            raise FilingDocumentConflictError(
                f"document_id conflict for {document_id[:12]}…"
            )

    by_pk = conn.execute(
        "SELECT filing_id, document_url, document_type, text"
        + (", document_id" if has_doc_id else "")
        + " FROM filing_documents WHERE filing_id = ? AND document_url = ?",
        (filing_id, document_url),
    ).fetchone()
    if by_pk is not None:
        existing_doc_id = by_pk[4] if has_doc_id and len(by_pk) > 4 else None
        if (
            by_pk[0] == filing_id
            and by_pk[1] == document_url
            and by_pk[2] == document_type
            and by_pk[3] == text
            and (not has_doc_id or existing_doc_id == document_id)
        ):
            return
        raise FilingDocumentConflictError(
            f"filing_documents PK conflict for {filing_id}/{document_url}"
        )

    if has_doc_id:
        conn.execute(
            """INSERT INTO filing_documents (
                filing_id, document_url, document_type, text, char_len,
                content_type, byte_size, extraction_status, extracted_at, document_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                filing_id,
                document_url,
                document_type,
                text,
                len(text),
                content_type,
                byte_size,
                "success",
                _now(),
                document_id,
            ),
        )
    else:
        conn.execute(
            """INSERT INTO filing_documents (
                filing_id, document_url, document_type, text, char_len,
                content_type, byte_size, extraction_status, extracted_at
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                filing_id,
                document_url,
                document_type,
                text,
                len(text),
                content_type,
                byte_size,
                "success",
                _now(),
            ),
        )


def materialize_sec_document(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    cell: Mapping[str, Any],
    fetch: FetchFn,
    filing_id: str | None = None,
) -> MaterializeResult:
    """Fetch one document via injected fetch; write raw/attempt/doc/provenance.

    filing_id is taken from the frozen cell identity_extensions (preferred).
    The optional filing_id kwarg is accepted only when it matches the cell.
    """
    ext = cell["identity_extensions"]
    document_id = ext["document_id"]
    url = ext["document_url"]
    plan_filing_id = ext.get("filing_id")
    if plan_filing_id:
        if filing_id is not None and filing_id != plan_filing_id:
            raise ValueError(
                "filing_id arg must match identity_extensions.filing_id from frozen plan"
            )
        filing_id = plan_filing_id
    elif not filing_id:
        raise ValueError(
            "filing_id missing from SEC v2 cell identity_extensions and not provided"
        )
    cell_id = cell["cell_id"]
    lfid = logical_fetch_id(run_id, cell_id)
    status_code, body, content_type = fetch(url)
    prior_attempts = conn.execute(
        "SELECT COUNT(*) FROM provider_request_attempts WHERE logical_fetch_id=?",
        (lfid,),
    ).fetchone()[0]
    request_id = sha256_identity(
        {"run_id": run_id, "url": url, "lfid": lfid, "attempt": prior_attempts + 1}
    )
    response_sha = _sha256_bytes(body)
    raw_asset_id = f"raw:{request_id}"

    params_json = __import__("json").dumps(
        {
            "ticker": cell["subject"],
            "window_start": cell["window_start"],
            "window_end": cell["window_end"],
            "page_no": 1,
            "endpoint_name": "sec_document",
            "cell_id": cell["cell_id"],
            "identity_schema_version": cell["identity_schema_version"],
            "inventory_id": ext["inventory_id"],
            "accession_number": ext["accession_number"],
            "document_id": document_id,
            "document_role": ext["document_role"],
            "document_file": ext["document_file"],
            "document_url": url,
            "requiredness": ext["requiredness"],
            "requiredness_reason": ext["requiredness_reason"],
            "filing_id": filing_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    fp = sha256_identity({"method": "GET", "url": url})
    started = _now()
    conn.execute(
        """INSERT OR IGNORE INTO provider_request_attempts (
            request_id, run_id, logical_fetch_id, source_type, provider,
            endpoint_name, ticker_or_series, window_start, window_end,
            attempt_no, page_no, request_fingerprint, request_params_redacted,
            started_at, status
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            request_id,
            run_id,
            lfid,
            "sec_filings",
            "sec",
            "sec_document",
            cell["subject"],
            cell["window_start"],
            cell["window_end"],
            1,
            1,
            fp,
            params_json,
            started,
            "STARTED",
        ),
    )
    if status_code == 200:
        conn.execute(
            """INSERT OR IGNORE INTO raw_assets (
                asset_id, ticker, source_type, reference_date, fetched_at,
                data_version, content_raw, response_sha256, request_id,
                page_no, content_encoding
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                raw_asset_id,
                cell["subject"],
                "sec_filings",
                cell["window_start"],
                _now(),
                "v2",
                body,
                response_sha,
                request_id,
                1,
                "identity",
            ),
        )
    terminal = "SUCCEEDED" if status_code == 200 else "HTTP_ERROR"
    conn.execute(
        """UPDATE provider_request_attempts SET
            completed_at=?, status=?, http_status=?, items_count=?,
            raw_asset_id=?, response_sha256=?, response_bytes=?
           WHERE request_id=?""",
        (
            _now(),
            terminal,
            status_code,
            1 if status_code == 200 else 0,
            raw_asset_id if status_code == 200 else None,
            response_sha if status_code == 200 else None,
            len(body),
            request_id,
        ),
    )
    request_count = conn.execute(
        "SELECT COUNT(*) FROM provider_request_attempts WHERE logical_fetch_id=?",
        (lfid,),
    ).fetchone()[0]

    if status_code != 200:
        return MaterializeResult(
            status="failed",
            logical_fetch_id=lfid,
            request_count=request_count,
            error=f"http_{status_code}",
        )

    is_primary = ext["document_role"] == "primary_doc"
    outcome = extract_document_text(
        body,
        content_type=content_type,
        is_primary=is_primary,
        requiredness=ext.get("requiredness", "mandatory"),
    )
    if outcome.status != "success":
        return MaterializeResult(
            status=outcome.status,
            document_id=document_id,
            raw_asset_id=raw_asset_id,
            logical_fetch_id=lfid,
            request_count=request_count,
            error=outcome.error_class,
        )

    text_sha = _sha256_bytes(outcome.text.encode("utf-8"))
    entity_version = sha256_identity(
        {
            "document_id": document_id,
            "response_sha256": response_sha,
            "extracted_text_sha256": text_sha,
            "extraction_normalizer_version": "sec_extract_v1",
        }
    )
    # ensure filing exists for FK if present
    conn.execute(
        """INSERT OR IGNORE INTO filings (
            filing_id, cik, ticker, form_type, filed_at, accession_number, url
        ) VALUES (?,?,?,?,?,?,?)""",
        (
            filing_id,
            "0000000000",
            cell["subject"],
            "8-K",
            cell["window_start"],
            ext["accession_number"],
            url,
        ),
    )
    _insert_filing_document_identity_aware(
        conn,
        filing_id=filing_id,
        document_url=url,
        document_type=ext["document_role"],
        text=outcome.text,
        content_type=content_type,
        byte_size=len(body),
        document_id=document_id,
    )
    conn.execute(
        """INSERT OR IGNORE INTO normalized_provenance (
            entity_type, entity_id, entity_version, raw_asset_id,
            normalizer_version, created_at
        ) VALUES ('filing',?,?,?,?,?)""",
        (document_id, entity_version, raw_asset_id, "sec_extract_v1", _now()),
    )
    return MaterializeResult(
        status="success",
        document_id=document_id,
        raw_asset_id=raw_asset_id,
        logical_fetch_id=lfid,
        request_count=request_count,
    )


def materialize_sec_index(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    cell: Mapping[str, Any],
    fetch: FetchFn,
    primary_document: str | None = None,
) -> MaterializeResult:
    """Try index URL fallback chain; ledger every request."""
    ext = cell["identity_extensions"]
    cik = ext["cik"]
    accession = ext["accession_number"]
    urls = filing_index_urls(cik_int=cik, accession=accession)
    cell_id = cell["cell_id"]
    lfid = logical_fetch_id(run_id, cell_id)
    last_status = "failed"
    for i, url in enumerate(urls, start=1):
        code, body, ctype = fetch(url)
        request_id = sha256_identity({"run_id": run_id, "url": url, "i": i})
        response_sha = _sha256_bytes(body) if body else None
        raw_asset_id = f"raw:{request_id}" if response_sha else None
        params_json = __import__("json").dumps(
            {"cik": cik, "accession_number": accession, "url": url},
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            """INSERT OR IGNORE INTO provider_request_attempts (
                request_id, run_id, logical_fetch_id, source_type, provider,
                endpoint_name, ticker_or_series, window_start, window_end,
                attempt_no, page_no, request_fingerprint, request_params_redacted,
                started_at, status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                request_id,
                run_id,
                lfid,
                "sec_filings",
                "sec",
                "sec_filing_index",
                cell["subject"],
                cell["window_start"],
                cell["window_end"],
                i,
                1,
                sha256_identity({"method": "GET", "url": url}),
                params_json,
                _now(),
                "STARTED",
            ),
        )
        if code == 200 and body:
            conn.execute(
                """INSERT OR IGNORE INTO raw_assets (
                    asset_id, ticker, source_type, reference_date, fetched_at,
                    data_version, content_raw, response_sha256, request_id,
                    page_no, content_encoding
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    raw_asset_id,
                    cell["subject"],
                    "sec_filings",
                    cell["window_start"],
                    _now(),
                    "v2",
                    body,
                    response_sha,
                    request_id,
                    1,
                    "identity",
                ),
            )
        conn.execute(
            """UPDATE provider_request_attempts SET
                completed_at=?, status=?, http_status=?, items_count=?,
                raw_asset_id=?, response_sha256=?, response_bytes=?
               WHERE request_id=?""",
            (
                _now(),
                "SUCCEEDED" if code == 200 else "HTTP_ERROR",
                code,
                1 if code == 200 else 0,
                raw_asset_id if code == 200 else None,
                response_sha,
                len(body or b""),
                request_id,
            ),
        )
        if code == 200 and body:
            parsed = parse_filing_index_html(
                body.decode("utf-8", errors="replace"),
                base_url=url.rsplit("/", 1)[0] + "/",
                primary_document=primary_document,
            )
            if parsed.status == "success":
                last_status = "success"
                break
    rc = conn.execute(
        "SELECT COUNT(*) FROM provider_request_attempts WHERE logical_fetch_id=?",
        (lfid,),
    ).fetchone()[0]
    return MaterializeResult(
        status=last_status, logical_fetch_id=lfid, request_count=rc
    )
