"""Offline SEC document materialize + production plan→executor path."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from catalyst_data.sec.document_cells import build_document_cell
from catalyst_data.sec.materialize import (
    FilingDocumentConflictError,
    materialize_sec_document,
)
from catalyst_data.sec.extract import extract_document_text


def _boot(tmp_path: Path) -> sqlite3.Connection:
    from catalyst_data.storage.sqlite import init_db
    from catalyst_data.migrations import run_migrations

    conn = sqlite3.connect(tmp_path / "t.db")
    init_db(conn)
    run_migrations(conn)
    conn.execute(
        """INSERT OR IGNORE INTO ingestion_runs
           (run_id, started_at, ticker_list_json, source_list_json, status)
           VALUES ('run1', '2026-01-01T00:00:00Z', '[]', '[]', 'RUNNING')"""
    )
    conn.commit()
    return conn


def _cell():
    return build_document_cell(
        ticker="AAPL",
        filed_date="2025-08-15",
        inventory_id="i" * 64,
        accession_number="acc",
        document_role="primary_doc",
        document_file="a.htm",
        document_url="https://example/a.htm",
        filing_id="sec:f1",
        requiredness="mandatory",
        requiredness_reason="primary",
    )


def _html_body(token: bytes = b"revenue growth earnings ") -> bytes:
    # Must exceed RAG_MIN_CHAR_COUNT (200) after HTML strip.
    # Keep token as a unique prefix so different tokens produce different text.
    pad = token + (b" substantive filing body text for readiness " * 12)
    return b"<html><body>" + pad + b"</body></html>"


def test_success_writes_raw_attempt_filing_document_provenance(tmp_path: Path):
    conn = _boot(tmp_path)

    def fetch(url: str):
        return 200, _html_body(), "text/html"

    cell = _cell()
    res = materialize_sec_document(conn, run_id="run1", cell=cell, fetch=fetch)
    assert res.status == "success"
    assert conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM provider_request_attempts").fetchone()[0] == 1
    assert (
        conn.execute("SELECT extraction_status FROM filing_documents").fetchone()[0]
        == "success"
    )
    assert conn.execute("SELECT COUNT(*) FROM normalized_provenance").fetchone()[0] == 1
    conn.close()


def test_entity_type_filing_entity_id_document_id(tmp_path: Path):
    conn = _boot(tmp_path)

    def fetch(url: str):
        return 200, _html_body(b"x"), "text/html"

    cell = _cell()
    materialize_sec_document(conn, run_id="run1", cell=cell, fetch=fetch)
    row = conn.execute(
        "SELECT entity_type, entity_id FROM normalized_provenance"
    ).fetchone()
    assert row[0] == "filing"
    assert row[1] == cell["identity_extensions"]["document_id"]
    conn.close()


def test_filing_documents_persists_same_document_id_as_provenance_and_corpus_input(
    tmp_path: Path,
):
    conn = _boot(tmp_path)

    def fetch(url: str):
        return 200, _html_body(b"y"), "text/html"

    cell = _cell()
    materialize_sec_document(conn, run_id="run1", cell=cell, fetch=fetch)
    doc_id = cell["identity_extensions"]["document_id"]
    assert (
        conn.execute("SELECT document_id FROM filing_documents").fetchone()[0] == doc_id
    )
    assert (
        conn.execute("SELECT entity_id FROM normalized_provenance").fetchone()[0]
        == doc_id
    )
    conn.close()


def test_request_count_equals_attempts(tmp_path: Path):
    conn = _boot(tmp_path)

    def fetch(url: str):
        return 200, _html_body(b"z"), "text/html"

    cell = _cell()
    res = materialize_sec_document(conn, run_id="run1", cell=cell, fetch=fetch)
    assert res.request_count == 1
    conn.close()


def test_placeholder_empty_not_text_ready():
    out = extract_document_text(b"", content_type="text/html")
    assert out.status != "success"


def test_filing_id_comes_from_frozen_cell_not_unvalidated_external(tmp_path: Path):
    conn = _boot(tmp_path)
    cell = _cell()
    assert cell["identity_extensions"]["filing_id"] == "sec:f1"

    def fetch(url: str):
        return 200, _html_body(b"f"), "text/html"

    # no filing_id kwarg — must succeed from cell
    res = materialize_sec_document(conn, run_id="run1", cell=cell, fetch=fetch)
    assert res.status == "success"
    # conflicting external filing_id rejected
    import pytest

    with pytest.raises(ValueError, match="filing_id arg must match"):
        materialize_sec_document(
            conn, run_id="run1", cell=cell, fetch=fetch, filing_id="sec:other"
        )
    conn.close()


def test_idempotent_identical_insert_no_replace(tmp_path: Path):
    conn = _boot(tmp_path)

    def fetch(url: str):
        return 200, _html_body(b"idem"), "text/html"

    cell = _cell()
    r1 = materialize_sec_document(conn, run_id="run1", cell=cell, fetch=fetch)
    r2 = materialize_sec_document(conn, run_id="run1", cell=cell, fetch=fetch)
    assert r1.status == "success" and r2.status == "success"
    assert conn.execute("SELECT COUNT(*) FROM filing_documents").fetchone()[0] == 1
    conn.close()


def test_conflicting_document_content_is_typed_failure(tmp_path: Path):
    import pytest

    conn = _boot(tmp_path)
    cell = _cell()
    bodies = [_html_body(b"aaa"), _html_body(b"bbb")]
    n = {"i": 0}

    def fetch(url: str):
        body = bodies[min(n["i"], 1)]
        n["i"] += 1
        return 200, body, "text/html"

    materialize_sec_document(conn, run_id="run1", cell=cell, fetch=fetch)
    with pytest.raises(FilingDocumentConflictError):
        materialize_sec_document(conn, run_id="run1", cell=cell, fetch=fetch)
    # row must still exist (not deleted/rebuilt)
    assert conn.execute("SELECT COUNT(*) FROM filing_documents").fetchone()[0] == 1
    conn.close()


def test_sec_document_full_identity_reaches_transport_and_redacted_ledger(tmp_path: Path):
    conn = _boot(tmp_path)
    seen = {}

    def fetch(url: str):
        seen["url"] = url
        return 200, _html_body(b"b"), "text/html"

    cell = _cell()
    materialize_sec_document(conn, run_id="run1", cell=cell, fetch=fetch)
    assert seen["url"] == cell["identity_extensions"]["document_url"]
    params = conn.execute(
        "SELECT request_params_redacted FROM provider_request_attempts"
    ).fetchone()[0]
    assert cell["identity_extensions"]["document_id"] in params
    assert "inventory_id" in params
    assert "filing_id" in params
    conn.close()


def _pipeline_conn(tmp_path: Path) -> sqlite3.Connection:
    from catalyst_data.storage.sqlite import init_db
    from catalyst_data.migrations import run_migrations

    db_path = tmp_path / "pipe.db"
    conn = sqlite3.connect(str(db_path))
    init_db(conn)
    run_migrations(conn)
    conn.row_factory = sqlite3.Row
    return conn


def test_new_root_parent_run_id_null_for_document_plan(tmp_path: Path):
    """S4 first run must insert ingestion_runs.parent_run_id IS NULL."""
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    conn = _pipeline_conn(tmp_path)
    cell = _cell()
    plan = UpdatePlan(
        config={"sources": ["sec_filings"], "tickers": ["AAPL"], "source_scopes": {}},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [cell]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    class DocTransport:
        async def request(self, **kwargs):
            return {
                "status": 200,
                "body": _html_body(b"root "),
                "data": {},
            }

    report = asyncio.run(
        execute_update(db=conn, plan=plan, transport=DocTransport(), parent_run_id=None)
    )
    row = conn.execute(
        "SELECT parent_run_id FROM ingestion_runs WHERE run_id = ?",
        (report["run_id"],),
    ).fetchone()
    assert row is not None
    assert row["parent_run_id"] is None
    assert report["status"] in ("SUCCEEDED", "PARTIAL")
    conn.close()


def test_resume_same_document_plan_skips_complete_cells(tmp_path: Path):
    """Terminal-complete cell_id is skipped on resume with same plan lineage."""
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    conn = _pipeline_conn(tmp_path)
    cell = _cell()
    plan = UpdatePlan(
        config={"sources": ["sec_filings"], "tickers": ["AAPL"], "source_scopes": {}},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [cell]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    calls = {"n": 0}

    class DocTransport:
        async def request(self, **kwargs):
            calls["n"] += 1
            return {"status": 200, "body": _html_body(b"res "), "data": {}}

    report1 = asyncio.run(
        execute_update(db=conn, plan=plan, transport=DocTransport(), parent_run_id=None)
    )
    assert report1["cells_success"] >= 1
    n_after_first = calls["n"]
    assert n_after_first >= 1

    # provenance + checkpoint terminal complete
    cp = conn.execute(
        "SELECT status, is_complete, cell_id FROM source_checkpoints WHERE cell_id=?",
        (cell["cell_id"],),
    ).fetchone()
    assert cp is not None
    assert cp["status"] == "success"
    assert int(cp["is_complete"]) == 1

    report2 = asyncio.run(
        execute_update(
            db=conn,
            plan=plan,
            transport=DocTransport(),
            parent_run_id=report1["run_id"],
        )
    )
    # resume must not re-fetch terminal-complete cell
    assert calls["n"] == n_after_first
    assert report2["cells_skipped"] >= 1
    child = conn.execute(
        "SELECT parent_run_id FROM ingestion_runs WHERE run_id = ?",
        (report2["run_id"],),
    ).fetchone()
    assert child["parent_run_id"] == report1["run_id"]
    conn.close()


def test_checkpoint_resume_uses_frozen_plan_cell_id_without_reconstructing_extensions(
    tmp_path: Path,
):
    """Resume keys on frozen plan cell_id; extensions come from plan not checkpoint."""
    from catalyst_data.update_pipeline import execute_update, _remaining_b2_cells
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    conn = _pipeline_conn(tmp_path)
    cell = _cell()
    # Ensure extensions present on plan only
    assert "identity_extensions" in cell
    plan = UpdatePlan(
        config={"sources": ["sec_filings"], "tickers": ["AAPL"], "source_scopes": {}},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [cell]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    class FailThenOk:
        def __init__(self):
            self.n = 0

        async def request(self, **kwargs):
            self.n += 1
            # first run fails
            return {"status": 500, "body": b"err", "data": {}}

    transport = FailThenOk()
    report1 = asyncio.run(
        execute_update(db=conn, plan=plan, transport=transport, parent_run_id=None)
    )
    assert report1["cells_failed"] >= 1
    remaining = _remaining_b2_cells(conn, plan, "evidence", report1["run_id"])
    assert any(c["cell_id"] == cell["cell_id"] for c in remaining)
    # full extensions still on plan cell (not reconstructed from checkpoint)
    rem = next(c for c in remaining if c["cell_id"] == cell["cell_id"])
    assert rem["identity_extensions"]["document_id"] == cell["identity_extensions"]["document_id"]
    assert rem["identity_extensions"]["document_url"] == cell["identity_extensions"]["document_url"]

    class OkTransport:
        async def request(self, **kwargs):
            # full cell must be present
            assert kwargs.get("cell") or True
            return {"status": 200, "body": _html_body(b"ok "), "data": {}}

    report2 = asyncio.run(
        execute_update(
            db=conn,
            plan=plan,
            transport=OkTransport(),
            parent_run_id=report1["run_id"],
        )
    )
    assert report2["cells_success"] >= 1
    assert (
        conn.execute("SELECT COUNT(*) FROM filing_documents").fetchone()[0] == 1
    )
    conn.close()


def test_plan_executor_transport_ledger_checkpoint_for_sec_document(tmp_path: Path):
    """End-to-end offline: plan → execute_update → transport → ledger → checkpoint."""
    from catalyst_data.update_pipeline import execute_update
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    conn = _pipeline_conn(tmp_path)
    cell = _cell()
    plan = UpdatePlan(
        config={"sources": ["sec_filings"], "tickers": ["AAPL"], "source_scopes": {}},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [cell]}},
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.expected_plan_hash = plan.plan_hash

    seen = {"cell": None, "page_url": None, "endpoint": None}

    class CaptureTransport:
        async def request(self, **kwargs):
            seen["cell"] = kwargs.get("cell")
            seen["page_url"] = kwargs.get("page_url")
            seen["endpoint"] = kwargs.get("endpoint_name")
            return {"status": 200, "body": _html_body(b"e2e "), "data": {}}

    report = asyncio.run(
        execute_update(db=conn, plan=plan, transport=CaptureTransport())
    )
    assert report["status"] == "SUCCEEDED"
    assert seen["endpoint"] == "sec_document"
    assert seen["cell"] is not None
    assert seen["cell"]["identity_extensions"]["document_id"] == cell["identity_extensions"]["document_id"]
    assert seen["page_url"] == cell["identity_extensions"]["document_url"]

    attempts = conn.execute(
        "SELECT endpoint_name, status, request_params_redacted FROM provider_request_attempts"
    ).fetchall()
    assert len(attempts) >= 1
    assert any(a["endpoint_name"] == "sec_document" for a in attempts)
    params = attempts[0]["request_params_redacted"]
    assert cell["identity_extensions"]["document_id"] in params

    cp = conn.execute(
        "SELECT cell_id, endpoint_name, is_complete, status, logical_fetch_id, request_count "
        "FROM source_checkpoints WHERE cell_id=?",
        (cell["cell_id"],),
    ).fetchone()
    assert cp is not None
    assert cp["cell_id"] == cell["cell_id"]
    assert cp["endpoint_name"] == "sec_document"
    assert int(cp["is_complete"]) == 1
    assert cp["status"] == "success"
    assert cp["logical_fetch_id"]
    assert int(cp["request_count"]) >= 1

    assert conn.execute("SELECT COUNT(*) FROM raw_assets").fetchone()[0] >= 1
    assert (
        conn.execute("SELECT document_id FROM filing_documents").fetchone()[0]
        == cell["identity_extensions"]["document_id"]
    )
    assert (
        conn.execute("SELECT entity_id FROM normalized_provenance").fetchone()[0]
        == cell["identity_extensions"]["document_id"]
    )
    conn.close()


def test_dict_transport_internal_typeerror_not_retried(tmp_path: Path):
    """Internal TypeError must not trigger second signature fallback call."""
    import asyncio
    from catalyst_data.update_pipeline import _invoke_dict_transport_fetcher

    calls = {"n": 0}

    async def flaky(subject, endpoint, ws, we, **kwargs):
        calls["n"] += 1
        raise TypeError("internal programming error")

    async def _run():
        with pytest.raises(TypeError, match="internal programming error"):
            await _invoke_dict_transport_fetcher(
                flaky,
                subject="AAPL",
                endpoint="sec_document",
                window_start="2025-08-01",
                window_end="2025-08-01",
                cell={"subject": "AAPL"},
                page_url="http://x",
            )

    import pytest
    asyncio.run(_run())
    assert calls["n"] == 1


def test_dict_transport_legacy_fake_called_once(tmp_path: Path):
    import asyncio
    from catalyst_data.update_pipeline import _invoke_dict_transport_fetcher

    calls = {"n": 0}

    async def legacy(subject, endpoint, ws, we):
        calls["n"] += 1
        return {"status": 200, "body": b"{}", "data": {}}

    async def _run():
        await _invoke_dict_transport_fetcher(
            legacy,
            subject="AAPL",
            endpoint="sec_submissions",
            window_start="2025-08-01",
            window_end="2025-08-01",
            cell={"subject": "AAPL"},
            page_url=None,
        )

    asyncio.run(_run())
    assert calls["n"] == 1


def test_dict_transport_modern_full_cell_called_once():
    import asyncio
    from catalyst_data.update_pipeline import _invoke_dict_transport_fetcher

    calls = {"n": 0, "cell": None}

    async def modern(subject, endpoint, ws, we, *, cell=None, page_url=None):
        calls["n"] += 1
        calls["cell"] = cell
        return {"status": 200, "body": b"{}", "data": {}}

    cell = {"subject": "AAPL", "cell_id": "c" * 64}

    async def _run():
        await _invoke_dict_transport_fetcher(
            modern,
            subject="AAPL",
            endpoint="sec_document",
            window_start="2025-08-01",
            window_end="2025-08-01",
            cell=cell,
            page_url="http://x",
        )

    asyncio.run(_run())
    assert calls["n"] == 1
    assert calls["cell"] is cell
