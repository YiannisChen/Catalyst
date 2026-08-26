"""M3 recovery-stage: recover-sec-primary offline tests (fake HTTP only).

Covers the supervisor-locked recover-sec-primary contract:
- missing-work predicate, URL reconstruction (existing archives formula)
- connector/parser/reparse persistence through existing contracts
- recovery-local retry (403/429/5xx/timeout, Retry-After honored, 404 terminal)
- checkpoint/resume, idempotency, conflict fail-closed
- report counters, denominator invariant, secret safety
All fetches are injected async fakes; no live network and no frozen DB.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from catalyst_data.config import RAG_MIN_CHAR_COUNT
from catalyst_data.connectors.base import FetchResult
from catalyst_data.sec.document_cells import compute_document_id
from catalyst_data.sec.extract import SEC_EXTRACT_PARSER_VERSION
from catalyst_data.sec.materialize import FilingDocumentConflictError
from catalyst_data.sec.recover_primary import (
    DEFAULT_MAX_ATTEMPTS,
    MAX_PRIMARY_DOCUMENT_BYTES,
    checkpoint_schema_version,
    compute_missing_work,
    fetch_document_with_retry,
    load_checkpoint,
    recover_primary_documents,
    reconstructed_document_url,
    write_checkpoint,
)

ACC = "0000320193-26-000001"
CIK = "0000320193"
TICKER = "AAPL"
PRIM = "aapl-20260328.htm"
URL = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/aapl-20260328.htm"
FILING_ID = f"sec:{CIK}:{ACC}"
RUN_ID = "r" * 64


def _h(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _html_body() -> bytes:
    text = "Apple Inc. reported results for the quarter ended March 28, 2026. " * 12
    assert len(text) >= RAG_MIN_CHAR_COUNT
    return ("<html><body><p>" + text + "</p></body></html>").encode()


def _fixture_conn() -> sqlite3.Connection:
    from catalyst_data.storage.sqlite import init_db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute(
        """INSERT OR IGNORE INTO ingestion_runs
           (run_id, started_at, ticker_list_json, source_list_json, status)
           VALUES (?, '2026-08-24T00:00:00Z', '[]', '["sec_filings"]', 'RUNNING')""",
        (RUN_ID,),
    )
    conn.commit()
    return conn


def _seed_filing(
    conn: sqlite3.Connection,
    *,
    acc: str = ACC,
    cik: str = CIK,
    prim: str = PRIM,
    url: str = URL,
    filing_id: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO filings (
               filing_id, cik, ticker, form_type, filed_at,
               accession_number, primary_document, url
           ) VALUES (?,?,?,?,?,?,?,?)""",
        (filing_id or f"sec:{cik}:{acc}", cik, TICKER, "8-K", "2026-03-28", acc, prim, url),
    )
    conn.commit()


def _seed_valid_primary(conn: sqlite3.Connection) -> str:
    document_id = compute_document_id(
        filing_id=FILING_ID,
        accession_number=ACC,
        document_role="primary",
        document_file=PRIM,
        document_url=URL,
    )
    conn.execute(
        """INSERT INTO filing_documents (
               filing_id, document_url, document_type, text, char_len,
               content_type, byte_size, extraction_status, extracted_at,
               document_id
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            FILING_ID,
            URL,
            "primary",
            "already present valid primary text that is long enough to matter",
            64,
            "text/html",
            128,
            "success",
            "2026-03-28T00:00:00Z",
            document_id,
        ),
    )
    conn.commit()
    return document_id


def _ok_fetch(body: bytes | None = None):
    body = _html_body() if body is None else body
    calls: list[str] = []

    async def fetch(url: str) -> FetchResult:
        calls.append(url)
        return FetchResult(
            status=200,
            data={
                "url": url,
                "raw_bytes": body,
                "content_type": "text/html",
                "byte_size": len(body),
            },
            source_label="sec:recovery_doc",
        )

    fetch.calls = calls
    return fetch


def _sequential_fetch(results: list[FetchResult]):
    calls: list[str] = []

    async def fetch(url: str) -> FetchResult:
        calls.append(url)
        return results.pop(0)

    fetch.calls = calls
    return fetch


class _SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _run(
    conn: sqlite3.Connection,
    fetch,
    *,
    accessions=None,
    checkpoint_path=None,
    **kwargs,
):
    return recover_primary_documents(
        conn,
        accessions=accessions or [ACC],
        fetch_document=fetch,
        run_id=RUN_ID,
        checkpoint_path=checkpoint_path,
        sleep=_SleepRecorder(),
        jitter=0.0,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# missing-work predicate / URL reconstruction
# ---------------------------------------------------------------------------


def test_compute_missing_work_counts_valid_and_missing():
    conn = _fixture_conn()
    _seed_filing(conn)
    _seed_valid_primary(conn)
    _seed_filing(conn, acc="0000320193-26-000002", url=URL.replace("000001", "000002"))
    missing, already_present = compute_missing_work(
        conn, [ACC, "0000320193-26-000002"]
    )
    assert already_present == 1
    assert len(missing) == 1
    assert missing[0]["accession_number"] == "0000320193-26-000002"


def test_compute_missing_work_fails_closed_on_unknown_accession():
    conn = _fixture_conn()
    with pytest.raises(ValueError):
        compute_missing_work(conn, [ACC])


def test_reconstructed_url_matches_archives_formula():
    conn = _fixture_conn()
    _seed_filing(conn)
    row = conn.execute("SELECT * FROM filings").fetchone()
    assert reconstructed_document_url(row) == URL


# ---------------------------------------------------------------------------
# success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_persists_row_raw_ledger_and_reparse_repairs(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    body = _html_body()
    fetch = _ok_fetch(body)
    counters = await _run(conn, fetch)

    assert counters["requested"] == 1
    assert counters["already_present"] == 0
    assert counters["succeeded"] == 1
    assert counters["remaining"] == 0
    assert counters["bytes_written"] == len(body)
    assert len(fetch.calls) == 1
    assert fetch.calls == [URL]

    row = conn.execute(
        "SELECT * FROM filing_documents WHERE filing_id=?", (FILING_ID,)
    ).fetchone()
    assert row["document_type"] == "primary"
    assert row["extraction_status"] == "success"
    assert row["document_id"] == compute_document_id(
        filing_id=FILING_ID,
        accession_number=ACC,
        document_role="primary",
        document_file=PRIM,
        document_url=URL,
    )
    assert row["parser_version"] == SEC_EXTRACT_PARSER_VERSION
    assert len(row["text"]) >= RAG_MIN_CHAR_COUNT
    expected_hash = hashlib.sha256(
        row["text"].encode("utf-8")
    ).hexdigest()
    assert row["document_hash"] == expected_hash

    raw = conn.execute(
        "SELECT * FROM raw_assets WHERE content_raw IS NOT NULL"
    ).fetchone()
    assert raw is not None
    assert raw["content_raw"] == body
    assert raw["response_sha256"] == _h(body)

    ledger = conn.execute(
        "SELECT * FROM provider_request_attempts WHERE run_id=?", (RUN_ID,)
    ).fetchone()
    assert ledger is not None
    assert ledger["status"] == "SUCCEEDED"
    assert ledger["http_status"] == 200
    assert URL in ledger["request_params_redacted"]

    prov = conn.execute(
        "SELECT * FROM normalized_provenance WHERE entity_type='filing'"
    ).fetchone()
    assert prov is not None
    assert prov["entity_id"] == row["document_id"]
    assert prov["raw_asset_id"] == raw["asset_id"]


@pytest.mark.asyncio
async def test_success_html_extracts_at_least_rag_min_chars(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    counters = await _run(conn, _ok_fetch())
    assert counters["succeeded"] == 1
    text = conn.execute(
        "SELECT text FROM filing_documents WHERE filing_id=?", (FILING_ID,)
    ).fetchone()["text"]
    assert len(text.strip()) >= RAG_MIN_CHAR_COUNT


# ---------------------------------------------------------------------------
# resume / idempotency / conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_skips_terminal_checkpoint(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    checkpoint = tmp_path / "ck.json"
    fetch = _ok_fetch()
    counters1 = await _run(conn, fetch, checkpoint_path=checkpoint)
    assert counters1["succeeded"] == 1
    ck = load_checkpoint(checkpoint)
    assert ck["statuses"][ACC]["status"] == "success"

    fetch2 = _ok_fetch()
    counters2 = await _run(conn, fetch2, checkpoint_path=checkpoint)
    assert counters2["succeeded"] == 0
    assert counters2["already_present"] == 1
    assert len(fetch2.calls) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM filing_documents"
    ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_resume_skips_terminal_checkpoint_for_missing_row(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    checkpoint = tmp_path / "ck.json"
    write_checkpoint(
        checkpoint,
        run_id=RUN_ID,
        statuses={ACC: {"status": "permanent_404", "http_status": 404}},
    )
    fetch = _ok_fetch()
    counters = await _run(conn, fetch, checkpoint_path=checkpoint)
    assert counters["succeeded"] == 0
    assert counters["permanent_404"] == 0
    assert counters["skipped_checkpoint"] == 1
    assert len(fetch.calls) == 0


@pytest.mark.asyncio
async def test_equal_rerun_is_idempotent(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    fetch1 = _ok_fetch()
    c1 = await _run(conn, fetch1)
    assert c1["succeeded"] == 1
    fetch2 = _ok_fetch()
    c2 = await _run(conn, fetch2)
    assert c2["already_present"] == 1
    assert c2["succeeded"] == 0
    assert len(fetch2.calls) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM filing_documents"
    ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_conflicting_rerun_fails_closed(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    document_id = compute_document_id(
        filing_id=FILING_ID,
        accession_number=ACC,
        document_role="primary",
        document_file=PRIM,
        document_url=URL,
    )
    # Existing row with the same identity but different persisted text and a
    # non-valid extraction status -> missing-work predicate true, fetch occurs,
    # insert conflicts on document_id/text.
    conn.execute(
        """INSERT INTO filing_documents (
               filing_id, document_url, document_type, text, char_len,
               content_type, byte_size, extraction_status, extracted_at,
               document_id
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            FILING_ID,
            URL,
            "primary",
            "OLD DIFFERENT BODY THAT NEVER MATCHES",
            38,
            "text/html",
            64,
            "fetch_failed",
            "2026-03-28T00:00:00Z",
            document_id,
        ),
    )
    conn.commit()
    with pytest.raises(FilingDocumentConflictError):
        await _run(conn, _ok_fetch())


# ---------------------------------------------------------------------------
# status handling / retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_is_terminal_no_retry(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    fetch = _sequential_fetch(
        [FetchResult(status=404, error="SEC doc 404", source_label="sec:recovery_doc")]
    )
    counters = await _run(conn, fetch)
    assert counters["permanent_404"] == 1
    assert counters["remaining"] == 0
    assert len(fetch.calls) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM filing_documents"
    ).fetchone()[0] == 0
    ledger = conn.execute(
        "SELECT * FROM provider_request_attempts WHERE run_id=?", (RUN_ID,)
    ).fetchone()
    assert ledger["status"] == "HTTP_ERROR"
    assert ledger["http_status"] == 404


@pytest.mark.asyncio
async def test_403_with_retry_after_then_success(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    fetch = _sequential_fetch(
        [
            FetchResult(
                status=403,
                error="SEC doc 403",
                retry_after_seconds=5.0,
                source_label="sec:recovery_doc",
            ),
            FetchResult(
                status=200,
                data={
                    "url": URL,
                    "raw_bytes": _html_body(),
                    "content_type": "text/html",
                    "byte_size": len(_html_body()),
                },
                source_label="sec:recovery_doc",
            ),
        ]
    )
    sleep = _SleepRecorder()
    counters = await recover_primary_documents(
        conn,
        accessions=[ACC],
        fetch_document=fetch,
        run_id=RUN_ID,
        sleep=sleep,
        jitter=0.0,
    )
    assert counters["succeeded"] == 1
    assert len(fetch.calls) == 2
    assert sleep.calls == [5.0]


@pytest.mark.asyncio
async def test_429_retry_after_honored(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    fetch = _sequential_fetch(
        [
            FetchResult(
                status=429,
                error="SEC doc 429",
                retry_after_seconds=30.0,
                source_label="sec:recovery_doc",
            ),
            FetchResult(
                status=200,
                data={
                    "url": URL,
                    "raw_bytes": _html_body(),
                    "content_type": "text/html",
                    "byte_size": len(_html_body()),
                },
                source_label="sec:recovery_doc",
            ),
        ]
    )
    sleep = _SleepRecorder()
    counters = await recover_primary_documents(
        conn,
        accessions=[ACC],
        fetch_document=fetch,
        run_id=RUN_ID,
        sleep=sleep,
        jitter=0.0,
    )
    assert counters["succeeded"] == 1
    assert sleep.calls == [30.0]


@pytest.mark.asyncio
async def test_5xx_retries_then_success(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    fetch = _sequential_fetch(
        [
            FetchResult(status=503, error="SEC doc 503", source_label="sec:recovery_doc"),
            FetchResult(status=503, error="SEC doc 503", source_label="sec:recovery_doc"),
            FetchResult(
                status=200,
                data={
                    "url": URL,
                    "raw_bytes": _html_body(),
                    "content_type": "text/html",
                    "byte_size": len(_html_body()),
                },
                source_label="sec:recovery_doc",
            ),
        ]
    )
    sleep = _SleepRecorder()
    counters = await recover_primary_documents(
        conn,
        accessions=[ACC],
        fetch_document=fetch,
        run_id=RUN_ID,
        sleep=sleep,
        jitter=0.0,
    )
    assert counters["succeeded"] == 1
    assert len(fetch.calls) == 3
    assert sleep.calls == [2.0, 4.0]


@pytest.mark.asyncio
async def test_timeout_retries_exhausted(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    fetch = _sequential_fetch(
        [
            FetchResult(status=0, error="Timeout: read timed out", source_label="sec:recovery_doc")
        ]
        * 5
    )
    sleep = _SleepRecorder()
    counters = await recover_primary_documents(
        conn,
        accessions=[ACC],
        fetch_document=fetch,
        run_id=RUN_ID,
        sleep=sleep,
        jitter=0.0,
    )
    assert counters["retry_exhausted"] == 1
    assert counters["remaining"] == 1
    assert len(fetch.calls) == DEFAULT_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# content outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_body_persists_empty_row(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    counters = await _run(conn, _ok_fetch(b""))
    assert counters["empty"] == 1
    row = conn.execute(
        "SELECT * FROM filing_documents WHERE filing_id=?", (FILING_ID,)
    ).fetchone()
    assert row["extraction_status"] == "empty"
    assert row["text"] is None
    assert row["parser_version"] == SEC_EXTRACT_PARSER_VERSION
    assert row["parse_quality"] == "not_applicable"


@pytest.mark.asyncio
async def test_malformed_body_below_min_chars_persists_empty_row(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    counters = await _run(conn, _ok_fetch(b"<html><body><p>tiny</p></body></html>"))
    assert counters["empty"] == 1
    row = conn.execute(
        "SELECT * FROM filing_documents WHERE filing_id=?", (FILING_ID,)
    ).fetchone()
    assert row["extraction_status"] == "empty"


@pytest.mark.asyncio
async def test_pdf_body_persists_pdf_skipped_row(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    body = b"%PDF-1.4 fake pdf primary"
    counters = await _run(conn, _ok_fetch(body))
    assert counters["pdf_skipped"] == 1
    row = conn.execute(
        "SELECT * FROM filing_documents WHERE filing_id=?", (FILING_ID,)
    ).fetchone()
    assert row["extraction_status"] == "pdf_skipped"
    assert row["text"] is None
    assert row["parse_quality"] == "not_applicable"


@pytest.mark.asyncio
async def test_oversized_body_rejected_content(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    counters = await _run(
        conn, _ok_fetch(b"x" * 64), max_document_bytes=32
    )
    assert counters["rejected_content"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM filing_documents"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM raw_assets WHERE content_raw IS NOT NULL"
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# URL / accession guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_host_fails_closed(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn, url="https://evil.example.com/Archives/edgar/data/x/y/z.htm")
    with pytest.raises(ValueError):
        await _run(conn, _ok_fetch())


@pytest.mark.asyncio
async def test_url_mismatch_fails_closed(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn, url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/other.htm")
    with pytest.raises(ValueError):
        await _run(conn, _ok_fetch())


# ---------------------------------------------------------------------------
# invariants and secrets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_denominator_invariant_filings_unchanged(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    before = conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
    await _run(conn, _ok_fetch())
    after = conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
    assert after == before
    assert after == 1


@pytest.mark.asyncio
async def test_checkpoint_never_contains_secret_headers(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    checkpoint = tmp_path / "ck.json"
    await _run(conn, _ok_fetch(), checkpoint_path=checkpoint)
    text = checkpoint.read_text(encoding="utf-8")
    assert "User-Agent" not in text
    assert "sk-" not in text
    payload = json.loads(text)
    assert payload["schema_version"] == checkpoint_schema_version
    assert payload["run_id"] == RUN_ID
    assert payload["statuses"][ACC]["status"] == "success"


@pytest.mark.asyncio
async def test_limit_bounds_work(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    _seed_filing(conn, acc="0000320193-26-000002", url=URL.replace("000001", "000002"))
    _seed_filing(conn, acc="0000320193-26-000003", url=URL.replace("000001", "000003"))
    counters = await _run(
        conn, _ok_fetch(), accessions=[ACC, "0000320193-26-000002", "0000320193-26-000003"], limit=2
    )
    assert counters["succeeded"] == 2
    assert counters["remaining"] == 1


@pytest.mark.asyncio
async def test_stop_check_breaks_after_persist_and_writes_checkpoint(tmp_path):
    conn = _fixture_conn()
    _seed_filing(conn)
    _seed_filing(conn, acc="0000320193-26-000002", url=URL.replace("000001", "000002"))
    checkpoint = tmp_path / "ck.json"
    stop = [False]
    counters = await recover_primary_documents(
        conn,
        accessions=[ACC, "0000320193-26-000002"],
        fetch_document=_ok_fetch(),
        run_id=RUN_ID,
        checkpoint_path=checkpoint,
        sleep=_SleepRecorder(),
        jitter=0.0,
        stop_check=lambda: stop[0],
    )
    # stop_check returned False the whole run; everything completes.
    assert counters["succeeded"] == 2
    assert counters["interrupted"] is False
    assert counters["remaining"] == 0

    stop2 = [True]
    conn2 = _fixture_conn()
    _seed_filing(conn2)
    _seed_filing(conn2, acc="0000320193-26-000002", url=URL.replace("000001", "000002"))
    ck2 = tmp_path / "ck2.json"
    counters2 = await recover_primary_documents(
        conn2,
        accessions=[ACC, "0000320193-26-000002"],
        fetch_document=_ok_fetch(),
        run_id=RUN_ID,
        checkpoint_path=ck2,
        sleep=_SleepRecorder(),
        jitter=0.0,
        stop_check=lambda: stop2[0],
    )
    assert counters2["succeeded"] == 1
    assert counters2["interrupted"] is True
    assert counters2["remaining"] == 1
    ck = load_checkpoint(ck2)
    assert ck["statuses"][ACC]["status"] == "success"
    assert "0000320193-26-000002" not in ck["statuses"]
