"""S4 sec_document cell identity and document_id."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from catalyst_data.manifests.universe import sha256_identity
from catalyst_data.sec.cell_record import build_sec_plan_cell_v2


def compute_document_id(
    *,
    filing_id: str,
    accession_number: str,
    document_role: str,
    document_file: str,
    document_url: str,
) -> str:
    return sha256_identity(
        {
            "filing_id": filing_id,
            "accession_number": accession_number,
            "document_role": document_role,
            "document_file": document_file,
            "document_url": document_url,
        }
    )


def build_document_cell(
    *,
    ticker: str,
    filed_date: str,
    inventory_id: str,
    accession_number: str,
    document_role: str,
    document_file: str,
    document_url: str,
    filing_id: str,
    requiredness: str,
    requiredness_reason: str,
    provider_profile_version: str = "v1",
) -> dict[str, Any]:
    document_id = compute_document_id(
        filing_id=filing_id,
        accession_number=accession_number,
        document_role=document_role,
        document_file=document_file,
        document_url=document_url,
    )
    return build_sec_plan_cell_v2(
        stage="evidence",
        source_type="sec_filings",
        endpoint_name="sec_document",
        subject=ticker,
        window_start=filed_date,
        window_end=filed_date,
        date_domain="as_of",
        provider_profile_version=provider_profile_version,
        identity_extensions={
            "inventory_id": inventory_id,
            "accession_number": accession_number,
            "document_id": document_id,
            "document_role": document_role,
            "document_file": document_file,
            "document_url": document_url,
            "requiredness": requiredness,
            "requiredness_reason": requiredness_reason,
            "filing_id": filing_id,
        },
    )


def compute_document_plan_hash(
    *,
    inventory_id: str,
    document_cells: Sequence[Mapping[str, Any]],
    executor_policy_version: str = "v1",
    retry_policy_version: str = "sec_v1",
    rate_policy_version: str = "sec_dev_v1",
) -> str:
    ordered_ids = [c["cell_id"] for c in document_cells]
    return sha256_identity(
        {
            "inventory_id": inventory_id,
            "document_cell_ids": ordered_ids,
            "executor_policy_version": executor_policy_version,
            "retry_policy_version": retry_policy_version,
            "rate_policy_version": rate_policy_version,
        }
    )
