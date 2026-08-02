"""S2 index cells and SEC plan-cell v2."""

from __future__ import annotations

import pytest

from catalyst_data.manifests.universe import SourceCell
from catalyst_data.sec.cell_record import build_sec_plan_cell_v2, validate_sec_plan_cell_v2
from catalyst_data.sec.index_cells import build_index_cell, compute_index_cell_id
from catalyst_data.manifests.universe import sha256_identity


def test_index_cell_id_includes_accession_cik_form_policy():
    cid = compute_index_cell_id(
        ticker="AAPL",
        filed_date="2025-08-15",
        accession_number="0001",
        cik="0000320193",
    )
    assert len(cid) == 64
    other = compute_index_cell_id(
        ticker="AAPL",
        filed_date="2025-08-15",
        accession_number="0002",
        cik="0000320193",
    )
    assert cid != other


def test_index_cell_distinct_from_submissions_cell():
    idx = build_index_cell(
        ticker="AAPL",
        filed_date="2025-08-15",
        accession_number="0001",
        cik="0000320193",
    )
    sub = SourceCell.create(
        stage="evidence",
        source_type="sec_filings",
        endpoint_name="sec_submissions",
        subject="AAPL",
        window_start="2026-07-23",
        window_end="2026-07-23",
        date_domain="as_of",
        provider_profile_version="v1",
    )
    assert idx["cell_id"] != sub.cell_id
    assert idx["endpoint_name"] == "sec_filing_index"


def test_no_document_discovery_in_index_module():
    import catalyst_data.sec.index_cells as m
    import inspect

    src = inspect.getsource(m)
    assert "http" not in src.lower() or "https://www.sec.gov" not in src


def test_sec_cell_v2_rejects_missing_and_unknown_extension_keys():
    with pytest.raises(ValueError):
        build_sec_plan_cell_v2(
            stage="evidence",
            source_type="sec_filings",
            endpoint_name="sec_filing_index",
            subject="AAPL",
            window_start="2025-08-15",
            window_end="2025-08-15",
            date_domain="as_of",
            provider_profile_version="v1",
            identity_extensions={"cik": "1"},  # missing keys
        )
    with pytest.raises(ValueError):
        validate_sec_plan_cell_v2(
            {
                "endpoint_name": "sec_filing_index",
                "identity_schema_version": "sec_cell_v2",
                "identity_extensions": {
                    "cik": "1",
                    "accession_number": "a",
                    "form_policy_version": "v1",
                    "extra": "nope",
                },
            }
        )


def test_sec_cell_v2_full_record_enters_plan_hash():
    cell = build_index_cell(
        ticker="AAPL",
        filed_date="2025-08-15",
        accession_number="0001",
        cik="0000320193",
    )
    plan_hash = sha256_identity({"cells": [cell]})
    cell2 = dict(cell)
    cell2["identity_extensions"] = {
        **cell2["identity_extensions"],
        "accession_number": "0002",
    }
    # recompute cell_id would change; plan with different accession differs
    plan_hash2 = sha256_identity(
        {
            "cells": [
                build_index_cell(
                    ticker="AAPL",
                    filed_date="2025-08-15",
                    accession_number="0002",
                    cik="0000320193",
                )
            ]
        }
    )
    assert plan_hash != plan_hash2


def test_legacy_source_cell_ids_and_serialization_unchanged():
    a = SourceCell.create(
        stage="evidence",
        source_type="sec_filings",
        endpoint_name="sec_submissions",
        subject="AAPL",
        window_start="2026-07-23",
        window_end="2026-07-23",
        date_domain="as_of",
        provider_profile_version="v1",
    )
    b = SourceCell.create(
        stage="evidence",
        source_type="sec_filings",
        endpoint_name="sec_submissions",
        subject="AAPL",
        window_start="2026-07-23",
        window_end="2026-07-23",
        date_domain="as_of",
        provider_profile_version="v1",
    )
    assert a.cell_id == b.cell_id
    assert a.to_identity() == b.to_identity()


def test_compute_index_cell_id_equals_build_index_cell_cell_id():
    kwargs = dict(
        ticker="AAPL",
        filed_date="2025-08-15",
        accession_number="0000320193-25-000001",
        cik="0000320193",
    )
    assert compute_index_cell_id(**kwargs) == build_index_cell(**kwargs)["cell_id"]


def test_planner_rejects_malformed_sec_cell_v2():
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    bad = {
        "stage": "evidence",
        "source_type": "sec_filings",
        "endpoint_name": "sec_filing_index",
        "subject": "AAPL",
        "window_start": "2025-08-15",
        "window_end": "2025-08-15",
        "date_domain": "as_of",
        "provider_profile_version": "v1",
        "page_cap": None,
        "item_cap": None,
        "identity_schema_version": "sec_cell_v2",
        "identity_extensions": {"cik": "1"},  # missing required keys
        "cell_id": "a" * 64,
    }
    plan = UpdatePlan(
        config={"sources": ["sec_filings"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [bad]}},
    )
    with pytest.raises(ValueError, match="missing identity_extensions"):
        compute_plan_hash(plan)


def test_planner_rejects_cell_id_full_record_mismatch():
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    cell = build_index_cell(
        ticker="AAPL",
        filed_date="2025-08-15",
        accession_number="0001",
        cik="0000320193",
    )
    bad = dict(cell)
    bad["cell_id"] = "0" * 64  # not equal to full-record hash
    plan = UpdatePlan(
        config={"sources": ["sec_filings"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [bad]}},
    )
    with pytest.raises(ValueError, match="cell_id mismatch"):
        compute_plan_hash(plan)


def test_planner_accepts_valid_sec_filing_index_and_sec_document_cells():
    from catalyst_data.sec.document_cells import build_document_cell
    from catalyst_data.update_planner import UpdatePlan, compute_plan_hash

    idx = build_index_cell(
        ticker="AAPL",
        filed_date="2025-08-15",
        accession_number="0001",
        cik="0000320193",
    )
    doc = build_document_cell(
        ticker="AAPL",
        filed_date="2025-08-15",
        inventory_id="i" * 64,
        accession_number="0001",
        document_role="primary_doc",
        document_file="a.htm",
        document_url="https://example/a.htm",
        filing_id="sec:f1",
        requiredness="mandatory",
        requiredness_reason="primary",
    )
    plan = UpdatePlan(
        config={"sources": ["sec_filings"], "tickers": ["AAPL"]},
        universe={"tickers": ["AAPL"]},
        stages={"market": {"cells": []}, "evidence": {"cells": [idx, doc]}},
    )
    h = compute_plan_hash(plan)
    assert len(h) == 64
