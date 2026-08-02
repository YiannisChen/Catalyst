"""S2 sec_filing_index cell identity."""

from __future__ import annotations

from typing import Any

from catalyst_data.manifests.universe import sha256_identity
from catalyst_data.sec.cell_record import build_sec_plan_cell_v2


def build_index_cell(
    *,
    ticker: str,
    filed_date: str,
    accession_number: str,
    cik: str,
    provider_profile_version: str = "v1",
    form_policy_version: str = "v1",
) -> dict[str, Any]:
    """Single canonical builder for S2 index cell identity."""
    return build_sec_plan_cell_v2(
        stage="evidence",
        source_type="sec_filings",
        endpoint_name="sec_filing_index",
        subject=ticker,
        window_start=filed_date,
        window_end=filed_date,
        date_domain="as_of",
        provider_profile_version=provider_profile_version,
        identity_extensions={
            "cik": cik,
            "accession_number": accession_number,
            "form_policy_version": form_policy_version,
        },
    )


def compute_index_cell_id(
    *,
    ticker: str,
    filed_date: str,
    accession_number: str,
    cik: str,
    provider_profile_version: str = "v1",
    form_policy_version: str = "v1",
) -> str:
    """Must equal build_index_cell(...)['cell_id'] for the same inputs."""
    return build_index_cell(
        ticker=ticker,
        filed_date=filed_date,
        accession_number=accession_number,
        cik=cik,
        provider_profile_version=provider_profile_version,
        form_policy_version=form_policy_version,
    )["cell_id"]
