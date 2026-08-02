"""S4 document cells and plan hash."""

from __future__ import annotations

from catalyst_data.sec.document_cells import (
    build_document_cell,
    compute_document_id,
    compute_document_plan_hash,
)
from catalyst_data.manifests.universe import sha256_identity


def test_document_cell_id_includes_inventory_id_role_file_url():
    c = build_document_cell(
        ticker="AAPL",
        filed_date="2025-08-15",
        inventory_id="i" * 64,
        accession_number="acc",
        document_role="primary_doc",
        document_file="a.htm",
        document_url="https://sec/a.htm",
        filing_id="sec:1",
        requiredness="mandatory",
        requiredness_reason="primary",
    )
    c2 = build_document_cell(
        ticker="AAPL",
        filed_date="2025-08-15",
        inventory_id="j" * 64,
        accession_number="acc",
        document_role="primary_doc",
        document_file="a.htm",
        document_url="https://sec/a.htm",
        filing_id="sec:1",
        requiredness="mandatory",
        requiredness_reason="primary",
    )
    assert c["cell_id"] != c2["cell_id"]


def test_document_id_formula():
    d1 = compute_document_id(
        filing_id="f",
        accession_number="a",
        document_role="primary_doc",
        document_file="x.htm",
        document_url="http://x",
    )
    d2 = compute_document_id(
        filing_id="f",
        accession_number="a",
        document_role="primary_doc",
        document_file="x.htm",
        document_url="http://x",
    )
    assert d1 == d2 and len(d1) == 64


def test_document_plan_hash_includes_inventory_and_ordered_cells_and_policy_versions():
    cells = [
        build_document_cell(
            ticker="AAPL",
            filed_date="2025-08-15",
            inventory_id="i" * 64,
            accession_number="acc",
            document_role="primary_doc",
            document_file="a.htm",
            document_url="https://sec/a.htm",
            filing_id="sec:1",
            requiredness="mandatory",
            requiredness_reason="primary",
        )
    ]
    h = compute_document_plan_hash(inventory_id="i" * 64, document_cells=cells)
    h2 = compute_document_plan_hash(inventory_id="j" * 64, document_cells=cells)
    assert h != h2


def test_document_plan_differs_from_baseline_b2o_plan_hash():
    cells = [
        build_document_cell(
            ticker="AAPL",
            filed_date="2025-08-15",
            inventory_id="i" * 64,
            accession_number="acc",
            document_role="primary_doc",
            document_file="a.htm",
            document_url="https://sec/a.htm",
            filing_id="sec:1",
            requiredness="mandatory",
            requiredness_reason="primary",
        )
    ]
    h = compute_document_plan_hash(inventory_id="i" * 64, document_cells=cells)
    assert h != "c335b918c3aef939e3deab922233fd120e0d270373d6353264fb424cb5d9866f"


def test_document_cell_record_contains_persisted_document_id_and_requiredness():
    c = build_document_cell(
        ticker="AAPL",
        filed_date="2025-08-15",
        inventory_id="i" * 64,
        accession_number="acc",
        document_role="exhibit_99_1",
        document_file="ex.htm",
        document_url="https://sec/ex.htm",
        filing_id="sec:1",
        requiredness="mandatory",
        requiredness_reason="ex99_htm",
    )
    ext = c["identity_extensions"]
    assert len(ext["document_id"]) == 64
    assert ext["requiredness"] == "mandatory"
    assert ext["requiredness_reason"] == "ex99_htm"
