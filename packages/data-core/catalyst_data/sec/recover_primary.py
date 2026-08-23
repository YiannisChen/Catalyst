"""SEC primary-document recovery for sealed DATA-01 accessions (M3 recovery stage).

Supervisor-locked 2026-08-24: recover all missing SEC primary documents on a
separately verified derivative before the offline prepare. This module owns the
offline-testable recovery logic (injected async transport; no live network in
tests) and the recovery-local retry policy (403/429/5xx/timeout, max 5
attempts, Retry-After honored, exponential + jitter fallback; 404 terminal).
It never redefines the parser or URL policy: URL reconstruction reuses the
existing EDGAR archives formula and parsing/persistence reuse
``sec.reparse.reparse_filing`` / ``persist_filing_document_reparse``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlparse

from catalyst_data.config import RatePolicy
from catalyst_data.connectors.base import FetchResult
from catalyst_data.corpus.news_v2 import _normalize_text
from catalyst_data.manifests.universe import sha256_identity
from catalyst_data.rate_limiter import TokenBucketLimiter
from catalyst_data.sec.document_cells import compute_document_id
from catalyst_data.sec.extract import (
    SEC_EXTRACT_PARSER_VERSION,
    extract_document_text,
)
from catalyst_data.sec.materialize import FilingDocumentConflictError
from catalyst_data.sec.reparse import (
    FilingParseResult,
    persist_filing_document_reparse,
    reparse_filing,
)

DEFAULT_MAX_ATTEMPTS = 5
MAX_PRIMARY_DOCUMENT_BYTES = 128 * 1024 * 1024
SEC_HOST = "www.sec.gov"
checkpoint_schema_version = "sec_primary_recovery_checkpoint_v1"

# Terminal checkpoint statuses are skipped on resume; every other status is
# retried. rejected_content is deterministic (oversized/unsupported bytes) and
# therefore also terminal to avoid re-fetching the same body.
TERMINAL_STATUSES = frozenset(
    {"success", "pdf_skipped", "empty", "permanent_404", "rejected_content"}
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def reconstructed_document_url(filing: Mapping[str, Any]) -> str:
    """Existing EDGAR archives formula (sec_normalize / b2o normalize)."""
    cik = str(filing["cik"] or "")
    acc = str(filing["accession_number"] or "")
    prim = str(filing["primary_document"] or "")
    if not cik or not acc or not prim:
        raise ValueError(
            f"cannot reconstruct primary URL for {acc}: cik/accession/primary_document required"
        )
    cik_int = cik.lstrip("0") or "0"
    nodash = acc.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{nodash}/{prim}"


def _validated_document_url(filing: Mapping[str, Any]) -> str:
    expected = reconstructed_document_url(filing)
    stored = str(filing["url"]) if "url" in filing.keys() else ""
    if stored != expected:
        raise ValueError(
            f"filings.url mismatch for {filing['accession_number']}: "
            "stored URL does not match the reconstructed archives URL"
        )
    parsed = urlparse(expected)
    if parsed.scheme != "https" or parsed.netloc != SEC_HOST:
        raise ValueError(
            f"non-SEC host for {filing['accession_number']}: {parsed.netloc!r}"
        )
    return expected


# ---------------------------------------------------------------------------
# missing-work predicate (mirrors gate_report row-level primary evidence)
# ---------------------------------------------------------------------------


def compute_missing_work(
    conn: sqlite3.Connection, accessions: Sequence[str]
) -> tuple[list[sqlite3.Row], int]:
    """Return (missing filing rows, already_present) over the sealed set A.

    A filing counts as already-present only when a primary document row has
    extraction_status='success', non-empty text, and a 64-hex document_id
    (the pre-canonical part of gate_report._primary_document_hash_bound).
    """
    missing: list[sqlite3.Row] = []
    already_present = 0
    for accession in accessions:
        filing = conn.execute(
            "SELECT * FROM filings WHERE accession_number=?", (accession,)
        ).fetchone()
        if filing is None:
            raise ValueError(
                f"sealed accession missing from derivative filings: {accession}"
            )
        valid = conn.execute(
            """SELECT 1 FROM filing_documents
               WHERE filing_id=?
                 AND document_type IN ('primary','primary_doc')
                 AND extraction_status='success'
                 AND length(trim(text)) > 0
                 AND document_id IS NOT NULL
                 AND length(document_id) = 64
                 AND document_id NOT GLOB '*[^0-9a-f]*'""",
            (filing["filing_id"],),
        ).fetchone()
        if valid is not None:
            already_present += 1
        else:
            missing.append(filing)
    return missing, already_present


# ---------------------------------------------------------------------------
# recovery-local retry (does not touch error_taxonomy / RETRY_POLICIES["sec"])
# ---------------------------------------------------------------------------


def _is_timeout(result: FetchResult) -> bool:
    return result.status == 0 and bool(result.error) and "timeout" in result.error.lower()


def _is_retryable(result: FetchResult) -> bool:
    if result.status in (403, 429):
        return True
    if 500 <= result.status <= 599:
        return True
    return _is_timeout(result)


def _retry_delay(result: FetchResult, attempt: int, *, jitter: float) -> float:
    retry_after = result.retry_after_seconds
    if retry_after is not None and retry_after > 0:
        return retry_after  # Retry-After honored; never capped below the header
    base = 2.0 * (2 ** (attempt - 1))
    if jitter > 0:
        base += random.uniform(0.0, jitter)
    return base


async def fetch_document_with_retry(
    raw_fetch: Callable[[str], Awaitable[FetchResult]],
    url: str,
    *,
    limiter: TokenBucketLimiter | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: float = 1.0,
) -> FetchResult:
    """Recovery-local fetch: retry 403/429/5xx/timeout up to max_attempts.

    404 and every other status are terminal (returned immediately). The
    limiter, when provided, is a single TokenBucketLimiter shared by the whole
    run so the global SEC fair-access rate is enforced across workers.
    """
    last: FetchResult | None = None
    for attempt in range(1, max_attempts + 1):
        if limiter is None:
            last = await raw_fetch(url)
        else:
            async with limiter.acquire():
                last = await raw_fetch(url)
        assert last is not None
        if last.status == 200:
            return last
        if _is_retryable(last) and attempt < max_attempts:
            await sleep(_retry_delay(last, attempt, jitter=jitter))
        else:
            return last
    assert last is not None
    return last


async def http_fetch_document(
    url: str,
    *,
    user_agent: str,
    client: Any = None,
) -> FetchResult:
    """Live recovery transport (mirrors the SEC connector headers/timeout).

    Captures Retry-After for the recovery-local retry loop (the connector's
    fetch_document does not populate retry_after_seconds). Never used as the
    stored extract: parsing always goes through sec.extract/sec.reparse.
    """
    import httpx

    own_client = client is None
    c = client if not own_client else httpx.AsyncClient(timeout=30.0)
    start = time.monotonic()
    try:
        resp = await c.get(
            url,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        )
        latency = (time.monotonic() - start) * 1000
    except (httpx.TimeoutException, httpx.ReadTimeout) as exc:
        latency = (time.monotonic() - start) * 1000
        return FetchResult(
            status=0,
            error=f"Timeout: {exc}",
            latency_ms=latency,
            source_label="sec:recovery_doc",
        )
    except httpx.HTTPError as exc:
        latency = (time.monotonic() - start) * 1000
        return FetchResult(
            status=0,
            error=str(exc),
            latency_ms=latency,
            source_label="sec:recovery_doc",
        )
    finally:
        if own_client:
            await c.aclose()

    retry_after = None
    if resp.status_code in (403, 429):
        raw_ra = resp.headers.get("retry-after")
        if raw_ra:
            try:
                retry_after = float(raw_ra)
            except (TypeError, ValueError):
                pass
    if resp.status_code == 200:
        return FetchResult(
            status=200,
            data={
                "url": url,
                "raw_bytes": resp.content,
                "content_type": resp.headers.get("content-type", ""),
                "byte_size": len(resp.content),
            },
            latency_ms=latency,
            source_label="sec:recovery_doc",
        )
    return FetchResult(
        status=resp.status_code,
        error=f"SEC doc {resp.status_code}",
        latency_ms=latency,
        retry_after_seconds=retry_after,
        source_label="sec:recovery_doc",
    )


# ---------------------------------------------------------------------------
# persistence (existing tables/contracts; atomic transaction per document)
# ---------------------------------------------------------------------------


def _document_identity(filing: Mapping[str, Any], url: str) -> str:
    return compute_document_id(
        filing_id=str(filing["filing_id"]),
        accession_number=str(filing["accession_number"]),
        document_role="primary",
        document_file=str(filing["primary_document"]),
        document_url=url,
    )


def _insert_filing_document_identity_aware(
    conn: sqlite3.Connection,
    *,
    filing_id: str,
    document_url: str,
    document_type: str,
    text: str | None,
    char_len: int,
    content_type: str | None,
    byte_size: int,
    extraction_status: str,
    document_id: str,
) -> None:
    """Mirror materialize._insert_filing_document_identity_aware semantics.

    Equal rerun is idempotent (identical row returns); any identity or content
    conflict raises FilingDocumentConflictError and rolls back the document.
    """
    by_doc = conn.execute(
        "SELECT filing_id, document_url, document_type, text, document_id "
        "FROM filing_documents WHERE document_id=?",
        (document_id,),
    ).fetchone()
    if by_doc is not None:
        if (
            by_doc["filing_id"] == filing_id
            and by_doc["document_url"] == document_url
            and by_doc["document_type"] == document_type
            and by_doc["text"] == text
            and by_doc["document_id"] == document_id
        ):
            return
        raise FilingDocumentConflictError(
            f"document_id conflict for {document_id[:12]}..."
        )
    by_pk = conn.execute(
        "SELECT filing_id, document_url, document_type, text, document_id "
        "FROM filing_documents WHERE filing_id=? AND document_url=?",
        (filing_id, document_url),
    ).fetchone()
    if by_pk is not None:
        if (
            by_pk["filing_id"] == filing_id
            and by_pk["document_url"] == document_url
            and by_pk["document_type"] == document_type
            and by_pk["text"] == text
            and by_pk["document_id"] == document_id
        ):
            return
        raise FilingDocumentConflictError(
            f"filing_documents PK conflict for {filing_id}/{document_url}"
        )
    conn.execute(
        """INSERT INTO filing_documents (
               filing_id, document_url, document_type, text, char_len,
               content_type, byte_size, extraction_status, extracted_at,
               document_id
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            filing_id,
            document_url,
            document_type,
            text,
            char_len,
            content_type,
            byte_size,
            extraction_status,
            _now_utc(),
            document_id,
        ),
    )


def _write_ledger(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    filing: Mapping[str, Any],
    url: str,
    document_id: str,
    result: FetchResult,
    lfid: str,
    request_id: str,
    raw_asset_id: str | None,
    response_sha: str | None,
    raw_bytes: bytes | None,
    started_at: str,
    terminal_status: str,
) -> None:
    params_json = json.dumps(
        {
            "ticker": filing["ticker"],
            "window_start": filing["filed_at"],
            "window_end": filing["filed_at"],
            "page_no": 1,
            "endpoint_name": "sec_document",
            "identity_schema_version": "sec_primary_recovery_v1",
            "accession_number": filing["accession_number"],
            "document_id": document_id,
            "document_role": "primary",
            "document_file": filing["primary_document"],
            "document_url": url,
            "requiredness": "mandatory",
            "requiredness_reason": "primary",
            "filing_id": filing["filing_id"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    fingerprint = sha256_identity({"method": "GET", "url": url})
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
            filing["ticker"],
            filing["filed_at"],
            filing["filed_at"],
            1,
            1,
            fingerprint,
            params_json,
            started_at,
            "STARTED",
        ),
    )
    http_status = result.status if 100 <= result.status <= 599 else None
    error_redacted = result.error
    if error_redacted and len(error_redacted) > 500:
        error_redacted = error_redacted[:500]
    conn.execute(
        """UPDATE provider_request_attempts SET
               completed_at=?, status=?, http_status=?, items_count=?,
               raw_asset_id=?, response_sha256=?, response_bytes=?,
               error_message_redacted=?, retry_after_seconds=?
           WHERE request_id=?""",
        (
            _now_utc(),
            terminal_status,
            http_status,
            1 if result.status == 200 else 0,
            raw_asset_id,
            response_sha,
            len(raw_bytes) if raw_bytes is not None else None,
            error_redacted,
            result.retry_after_seconds,
            request_id,
        ),
    )


def _write_raw_asset(
    conn: sqlite3.Connection,
    *,
    filing: Mapping[str, Any],
    request_id: str,
    raw_bytes: bytes,
    response_sha: str,
) -> str:
    raw_asset_id = f"raw:{request_id}"
    conn.execute(
        """INSERT OR IGNORE INTO raw_assets (
            asset_id, ticker, source_type, reference_date, fetched_at,
            data_version, content_raw, response_sha256, request_id,
            page_no, content_encoding
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            raw_asset_id,
            filing["ticker"],
            "sec_filings",
            filing["filed_at"],
            _now_utc(),
            "v2",
            raw_bytes,
            response_sha,
            request_id,
            1,
            "identity",
        ),
    )
    return raw_asset_id


def _write_provenance(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    response_sha: str,
    extracted_text_sha: str,
    raw_asset_id: str,
) -> None:
    entity_version = sha256_identity(
        {
            "document_id": document_id,
            "response_sha256": response_sha,
            "extracted_text_sha256": extracted_text_sha,
            "extraction_normalizer_version": SEC_EXTRACT_PARSER_VERSION,
        }
    )
    conn.execute(
        """INSERT OR IGNORE INTO normalized_provenance (
            entity_type, entity_id, entity_version, raw_asset_id,
            normalizer_version, created_at
        ) VALUES ('filing',?,?,?,?,?)""",
        (document_id, entity_version, raw_asset_id, SEC_EXTRACT_PARSER_VERSION, _now_utc()),
    )


def _persist_attempt_only(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    filing: Mapping[str, Any],
    url: str,
    document_id: str,
    result: FetchResult,
    checkpoint_status: str,
) -> dict[str, Any]:
    """Ledger-only persistence for terminal HTTP outcomes (no raw/doc rows)."""
    started_at = _now_utc()
    lfid = _logical_fetch_id(run_id, filing["accession_number"])
    request_id = _request_id(run_id, url, lfid)
    conn.execute("BEGIN")
    try:
        _write_ledger(
            conn,
            run_id=run_id,
            filing=filing,
            url=url,
            document_id=document_id,
            result=result,
            lfid=lfid,
            request_id=request_id,
            raw_asset_id=None,
            response_sha=None,
            raw_bytes=None,
            started_at=started_at,
            terminal_status="HTTP_ERROR",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "status": checkpoint_status,
        "http_status": result.status,
        "document_id": document_id,
        "bytes_written": 0,
    }


def _persist_document(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    filing: Mapping[str, Any],
    url: str,
    raw_bytes: bytes,
    content_type: str | None,
    result: FetchResult,
) -> dict[str, Any]:
    """Persist one fetched primary through extract/reparse and existing tables.

    Atomic per document: raw_assets + provider_request_attempts +
    filing_documents + normalized_provenance + M3-4 repair columns commit
    together (persist_filing_document_reparse performs the commit). Equal
    reruns are idempotent; conflicting bytes fail closed.
    """
    accession = str(filing["accession_number"])
    document_id = _document_identity(filing, url)
    outcome = extract_document_text(
        raw_bytes,
        content_type=content_type,
        is_primary=True,
        parser_version=SEC_EXTRACT_PARSER_VERSION,
    )
    if outcome.status == "success":
        reparse = reparse_filing(
            raw_bytes, accession, parser_version=SEC_EXTRACT_PARSER_VERSION
        )
        stored_text = _normalize_text(outcome.text)
        response_sha = _sha256_bytes(raw_bytes)
        started_at = _now_utc()
        lfid = _logical_fetch_id(run_id, accession)
        request_id = _request_id(run_id, url, lfid)
        raw_asset_id = f"raw:{request_id}"
        conn.execute("BEGIN")
        try:
            _write_raw_asset(
                conn,
                filing=filing,
                request_id=request_id,
                raw_bytes=raw_bytes,
                response_sha=response_sha,
            )
            _write_ledger(
                conn,
                run_id=run_id,
                filing=filing,
                url=url,
                document_id=document_id,
                result=result,
                lfid=lfid,
                request_id=request_id,
                raw_asset_id=raw_asset_id,
                response_sha=response_sha,
                raw_bytes=raw_bytes,
                started_at=started_at,
                terminal_status="SUCCEEDED",
            )
            _insert_filing_document_identity_aware(
                conn,
                filing_id=str(filing["filing_id"]),
                document_url=url,
                document_type="primary",
                text=stored_text,
                char_len=len(stored_text),
                content_type=content_type,
                byte_size=len(raw_bytes),
                extraction_status="success",
                document_id=document_id,
            )
            _write_provenance(
                conn,
                document_id=document_id,
                response_sha=response_sha,
                extracted_text_sha=_sha256_bytes(stored_text.encode("utf-8")),
                raw_asset_id=raw_asset_id,
            )
            persist_filing_document_reparse(
                conn,
                filing_id=str(filing["filing_id"]),
                document_id=document_id,
                result=reparse,
                extracted_text=stored_text,
            )
        except Exception:
            conn.rollback()
            raise
        return {
            "status": "success",
            "http_status": 200,
            "document_id": document_id,
            "bytes_written": len(raw_bytes),
        }

    row_status = (
        "pdf_skipped"
        if outcome.status in ("pdf_skipped", "mandatory_failed")
        else "empty"
    )
    response_sha = _sha256_bytes(raw_bytes)
    started_at = _now_utc()
    lfid = _logical_fetch_id(run_id, accession)
    request_id = _request_id(run_id, url, lfid)
    raw_asset_id = f"raw:{request_id}"
    not_applicable = FilingParseResult(
        accession=accession,
        parser_version=SEC_EXTRACT_PARSER_VERSION,
        primary_document_extracted=False,
        document_hash=None,
        parse_quality="not_applicable",
        sections=(),
    )
    conn.execute("BEGIN")
    try:
        _write_raw_asset(
            conn,
            filing=filing,
            request_id=request_id,
            raw_bytes=raw_bytes,
            response_sha=response_sha,
        )
        _write_ledger(
            conn,
            run_id=run_id,
            filing=filing,
            url=url,
            document_id=document_id,
            result=result,
            lfid=lfid,
            request_id=request_id,
            raw_asset_id=raw_asset_id,
            response_sha=response_sha,
            raw_bytes=raw_bytes,
            started_at=started_at,
            terminal_status="SUCCEEDED",
        )
        _insert_filing_document_identity_aware(
            conn,
            filing_id=str(filing["filing_id"]),
            document_url=url,
            document_type="primary",
            text=None,
            char_len=0,
            content_type=content_type,
            byte_size=len(raw_bytes),
            extraction_status=row_status,
            document_id=document_id,
        )
        persist_filing_document_reparse(
            conn,
            filing_id=str(filing["filing_id"]),
            document_id=document_id,
            result=not_applicable,
            extracted_text=None,
        )
    except Exception:
        conn.rollback()
        raise
    return {
        "status": row_status,
        "http_status": 200,
        "document_id": document_id,
        "bytes_written": len(raw_bytes),
    }


def _logical_fetch_id(run_id: str, accession: str) -> str:
    from catalyst_data.ingestion.request_ledger import compute_logical_fetch_id

    return compute_logical_fetch_id(run_id, f"sec_primary:{accession}")


def _request_id(run_id: str, url: str, lfid: str) -> str:
    return sha256_identity(
        {"run_id": run_id, "url": url, "lfid": lfid, "attempt": 1}
    )


# ---------------------------------------------------------------------------
# checkpoint
# ---------------------------------------------------------------------------


def load_checkpoint(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    if path is None or not Path(path).exists():
        return {"schema_version": checkpoint_schema_version, "run_id": None, "statuses": {}}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("statuses"), dict):
        raise ValueError("checkpoint is malformed; refusing to resume")
    return payload


def write_checkpoint(
    path: str | os.PathLike[str],
    *,
    run_id: str,
    statuses: Mapping[str, Any],
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    payload = {
        "schema_version": checkpoint_schema_version,
        "run_id": run_id,
        "statuses": dict(statuses),
        "updated_at": _now_utc(),
    }
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


def _empty_counters() -> dict[str, Any]:
    return {
        "requested": 0,
        "already_present": 0,
        "succeeded": 0,
        "pdf_skipped": 0,
        "empty": 0,
        "permanent_404": 0,
        "retry_exhausted": 0,
        "transient_failed": 0,
        "http_403": 0,
        "rejected_content": 0,
        "skipped_checkpoint": 0,
        "remaining": 0,
        "bytes_written": 0,
        "requests_made": 0,
        "interrupted": False,
    }


async def recover_primary_documents(
    conn: sqlite3.Connection,
    *,
    accessions: Sequence[str],
    fetch_document: Callable[[str], Awaitable[FetchResult]],
    run_id: str,
    checkpoint_path: str | os.PathLike[str] | None = None,
    limiter: TokenBucketLimiter | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_document_bytes: int = MAX_PRIMARY_DOCUMENT_BYTES,
    limit: int | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: float = 1.0,
    stop_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Recover missing primary documents over sealed accessions.

    Writes only through existing persistence contracts on the caller-provided
    (derivative) connection. The checkpoint file is the resume state; terminal
    statuses are skipped and retryable statuses are retried on the next run.
    """
    missing, already_present = compute_missing_work(conn, accessions)
    counters = _empty_counters()
    counters["requested"] = len(missing)
    counters["already_present"] = already_present

    checkpoint = load_checkpoint(checkpoint_path)
    statuses: dict[str, Any] = dict(checkpoint.get("statuses") or {})
    attempts: list[int] = [0]

    async def counted_fetch(url: str) -> FetchResult:
        attempts[0] += 1
        return await fetch_document(url)

    conn.execute(
        """INSERT OR IGNORE INTO ingestion_runs (
            run_id, started_at, ticker_list_json, source_list_json, status
        ) VALUES (?,?,?,?,?)""",
        (run_id, _now_utc(), "[]", '["sec_filings"]', "RUNNING"),
    )
    conn.commit()

    attempted = 0
    for filing in missing:
        accession = str(filing["accession_number"])
        prior = statuses.get(accession)
        if prior is not None and prior.get("status") in TERMINAL_STATUSES:
            counters["skipped_checkpoint"] += 1
            continue
        if limit is not None and attempted >= limit:
            break
        attempted += 1

        url = _validated_document_url(filing)
        document_id = _document_identity(filing, url)
        result = await fetch_document_with_retry(
            counted_fetch,
            url,
            limiter=limiter,
            max_attempts=max_attempts,
            sleep=sleep,
            jitter=jitter,
        )

        if result.status == 404:
            outcome = _persist_attempt_only(
                conn,
                run_id=run_id,
                filing=filing,
                url=url,
                document_id=document_id,
                result=result,
                checkpoint_status="permanent_404",
            )
        elif result.status == 200:
            data = result.data if isinstance(result.data, dict) else {}
            raw_bytes = data.get("raw_bytes") or result.raw_body or b""
            content_type = data.get("content_type")
            if len(raw_bytes) > max_document_bytes:
                outcome = _persist_attempt_only(
                    conn,
                    run_id=run_id,
                    filing=filing,
                    url=url,
                    document_id=document_id,
                    result=result,
                    checkpoint_status="rejected_content",
                )
            else:
                outcome = _persist_document(
                    conn,
                    run_id=run_id,
                    filing=filing,
                    url=url,
                    raw_bytes=raw_bytes,
                    content_type=content_type,
                    result=result,
                )
        elif result.status in (403, 429) or 500 <= result.status <= 599 or _is_timeout(result):
            checkpoint_status = (
                "http_403" if result.status == 403 else "retry_exhausted"
            )
            outcome = _persist_attempt_only(
                conn,
                run_id=run_id,
                filing=filing,
                url=url,
                document_id=document_id,
                result=result,
                checkpoint_status=checkpoint_status,
            )
        else:
            outcome = _persist_attempt_only(
                conn,
                run_id=run_id,
                filing=filing,
                url=url,
                document_id=document_id,
                result=result,
                checkpoint_status="transient_failed",
            )

        status_name = outcome["status"]
        _STATUS_TO_COUNTER = {
            "success": "succeeded",
            "pdf_skipped": "pdf_skipped",
            "empty": "empty",
            "permanent_404": "permanent_404",
            "retry_exhausted": "retry_exhausted",
            "transient_failed": "transient_failed",
            "http_403": "http_403",
            "rejected_content": "rejected_content",
        }
        counters[_STATUS_TO_COUNTER[status_name]] += 1
        counters["bytes_written"] += outcome.get("bytes_written", 0)
        statuses[accession] = {
            "status": status_name,
            "http_status": outcome.get("http_status"),
            "document_id": outcome.get("document_id"),
            "updated_at": _now_utc(),
        }
        if checkpoint_path is not None:
            write_checkpoint(checkpoint_path, run_id=run_id, statuses=statuses)
        if stop_check is not None and stop_check():
            counters["interrupted"] = True
            break

    counters["requests_made"] = attempts[0]
    terminal_done = (
        counters["succeeded"]
        + counters["pdf_skipped"]
        + counters["empty"]
        + counters["permanent_404"]
        + counters["rejected_content"]
        + counters["skipped_checkpoint"]
    )
    counters["remaining"] = counters["requested"] - terminal_done

    # Post-run invariant: PDF/empty rows are persisted rows, so the total
    # filing_documents count must equal already_present + every row type the
    # recovery writes (success/pdf_skipped/empty).
    if counters["pdf_skipped"] or counters["empty"]:
        fd_count = int(
            conn.execute("SELECT COUNT(*) FROM filing_documents").fetchone()[0]
        )
        expected = (
            counters["already_present"]
            + counters["succeeded"]
            + counters["pdf_skipped"]
            + counters["empty"]
        )
        if fd_count != expected:
            raise ValueError(
                f"filing_documents invariant violated: {fd_count} != {expected}"
            )
    return counters


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "MAX_PRIMARY_DOCUMENT_BYTES",
    "TERMINAL_STATUSES",
    "checkpoint_schema_version",
    "compute_missing_work",
    "fetch_document_with_retry",
    "http_fetch_document",
    "load_checkpoint",
    "recover_primary_documents",
    "reconstructed_document_url",
    "write_checkpoint",
]
