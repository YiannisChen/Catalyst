"""Pre-B6 SEC discovery, inventory, document identity (offline modules)."""

from .cell_record import (
    SEC_CELL_V2,
    build_sec_plan_cell_v2,
    validate_sec_plan_cell_v2,
)
from .convergence_identity import (
    compute_convergence_plan_hash,
    compute_reconciliation_evidence_hash,
)
from .document_cells import (
    build_document_cell,
    compute_document_id,
    compute_document_plan_hash,
)
from .extract import ExtractOutcome, extract_document_text
from .index_cells import build_index_cell, compute_index_cell_id
from .index_parser import IndexParseResult, parse_filing_index_html
from .inventory import (
    CANONICAL_END,
    CANONICAL_START,
    CARRY_IN_FLOOR,
    build_filing_inventory_manifest,
    compute_inventory_id,
    requiredness_for_descriptor,
)

__all__ = [
    "SEC_CELL_V2",
    "build_sec_plan_cell_v2",
    "validate_sec_plan_cell_v2",
    "compute_convergence_plan_hash",
    "compute_reconciliation_evidence_hash",
    "build_document_cell",
    "compute_document_id",
    "compute_document_plan_hash",
    "ExtractOutcome",
    "extract_document_text",
    "build_index_cell",
    "compute_index_cell_id",
    "IndexParseResult",
    "parse_filing_index_html",
    "CANONICAL_END",
    "CANONICAL_START",
    "CARRY_IN_FLOOR",
    "build_filing_inventory_manifest",
    "compute_inventory_id",
    "requiredness_for_descriptor",
]
