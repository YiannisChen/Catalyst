"""Filing inventory identity and requiredness tests."""

from __future__ import annotations

from pathlib import Path

from catalyst_data.manifests.universe import load_universe_spec
from catalyst_data.sec.inventory import (
    build_filing_inventory_manifest,
    compute_inventory_id,
    requiredness_for_descriptor,
    select_carry_in,
    US_PERIODIC_10Q,
)

_SPEC = load_universe_spec(
    Path(__file__).parents[1]
    / "catalyst_data"
    / "manifests"
    / "universe_v1_2025_08.spec.json"
)


def _inventory_args():
    return {
        "tickers": list(_SPEC.tickers),
        "issuer_class_by_ticker": {
            ticker: _SPEC.companies[ticker]["issuer_class"]
            for ticker in _SPEC.tickers
        },
    }


def _docs():
    return [
        {
            "filename": "aapl-20250801.htm",
            "document_file": "aapl-20250801.htm",
            "document_url": "https://sec/aapl-20250801.htm",
            "document_type": "8-K",
            "sequence": 1,
            "content_extension": "htm",
            "is_primary": True,
        },
        {
            "filename": "ex99-2.htm",
            "document_file": "ex99-2.htm",
            "document_url": "https://sec/ex99-2.htm",
            "document_type": "EX-99.2",
            "sequence": 3,
            "content_extension": "htm",
            "is_primary": False,
        },
        {
            "filename": "ex99-1.htm",
            "document_file": "ex99-1.htm",
            "document_url": "https://sec/ex99-1.htm",
            "document_type": "EX-99.1",
            "sequence": 2,
            "content_extension": "htm",
            "is_primary": False,
        },
    ]


def test_inventory_id_excludes_document_plan_hash():
    m1 = build_filing_inventory_manifest(
        source_snapshot_id="s" * 64,
        universe_manifest_id="u" * 64,
        **_inventory_args(),
        filing_entries=[
            {
                "ticker": "AAPL",
                "accession_number": "0001",
                "documents": _docs(),
            }
        ],
    )
    body = {k: v for k, v in m1.items() if k != "inventory_id"}
    body["document_plan_hash"] = "x" * 64
    # compute without document_plan_hash field in identity
    assert compute_inventory_id(
        {k: v for k, v in m1.items() if k != "inventory_id"}
    ) == m1["inventory_id"]


def test_inventory_id_stable_for_fixed_entries():
    a = build_filing_inventory_manifest(
        source_snapshot_id="s" * 64,
        universe_manifest_id="u" * 64,
        **_inventory_args(),
        filing_entries=[{"ticker": "AAPL", "accession_number": "1", "documents": _docs()}],
    )
    b = build_filing_inventory_manifest(
        source_snapshot_id="s" * 64,
        universe_manifest_id="u" * 64,
        **_inventory_args(),
        filing_entries=[{"ticker": "AAPL", "accession_number": "1", "documents": _docs()}],
    )
    assert a["inventory_id"] == b["inventory_id"]
    assert len(a["inventory_id"]) == 64


def test_document_sort_primary_then_sequence():
    m = build_filing_inventory_manifest(
        source_snapshot_id="s" * 64,
        universe_manifest_id="u" * 64,
        **_inventory_args(),
        filing_entries=[{"ticker": "AAPL", "accession_number": "1", "documents": _docs()}],
    )
    docs = m["sorted_filing_entries"][0]["documents"]
    assert docs[0]["is_primary"] is True
    assert docs[1]["filename"] == "ex99-1.htm"
    assert docs[2]["filename"] == "ex99-2.htm"


def test_ex99_roles_are_exhibit_99_n_not_all_1():
    m = build_filing_inventory_manifest(
        source_snapshot_id="s" * 64,
        universe_manifest_id="u" * 64,
        **_inventory_args(),
        filing_entries=[{"ticker": "AAPL", "accession_number": "1", "documents": _docs()}],
    )
    roles = [d["document_role"] for d in m["sorted_filing_entries"][0]["documents"]]
    assert roles[0] == "primary"
    assert roles[1] == "exhibit_99_1"
    assert roles[2] == "exhibit_99_2"


def test_carry_in_periodic_only():
    filings = [
        {"form_type": "10-Q", "filed_date": "2024-06-01"},
        {"form_type": "10-Q", "filed_date": "2025-03-01"},
        {"form_type": "8-K", "filed_date": "2024-05-01"},
    ]
    picked = select_carry_in(filings, form_class=US_PERIODIC_10Q)
    assert picked is not None
    assert picked["filed_date"] == "2025-03-01"


def test_missing_carry_in_slot_recorded():
    assert select_carry_in([], form_class=US_PERIODIC_10Q) is None


def test_requiredness_and_reason_enter_inventory_id():
    m = build_filing_inventory_manifest(
        source_snapshot_id="s" * 64,
        universe_manifest_id="u" * 64,
        **_inventory_args(),
        filing_entries=[{"ticker": "AAPL", "accession_number": "1", "documents": _docs()}],
    )
    docs = m["sorted_filing_entries"][0]["documents"]
    assert docs[0]["requiredness"] == "mandatory"
    assert "requiredness" in str(docs)


def test_response_cannot_downgrade_mandatory_to_optional():
    # Policy: requiredness computed from descriptor extension, not response
    r, reason = requiredness_for_descriptor(
        is_primary=False, content_extension="htm", form_type="8-K", document_type="EX-99.1"
    )
    assert r == "mandatory"
    # response PDF would fail at extract as mandatory_failed, not optional


def test_unknown_ex99_extension_is_mandatory():
    r, reason = requiredness_for_descriptor(
        is_primary=False, content_extension="xyz", form_type="8-K", document_type="EX-99.1"
    )
    assert r == "mandatory"
    assert reason == "ex99_unknown_ext"


def test_pdf_ex99_descriptor_is_optional_degraded():
    r, reason = requiredness_for_descriptor(
        is_primary=False, content_extension="pdf", form_type="8-K", document_type="EX-99.1"
    )
    assert r == "optional_degraded"
    assert reason == "ex99_pdf"


def test_primary_pdf_always_blocks_source_ready():
    r, reason = requiredness_for_descriptor(
        is_primary=True, content_extension="pdf", form_type="10-K", document_type="10-K"
    )
    assert r == "mandatory"
