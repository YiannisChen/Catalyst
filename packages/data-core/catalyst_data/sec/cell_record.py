"""SEC plan-cell record v2 — full identity without legacy SourceCell drift."""

from __future__ import annotations

from typing import Any, Mapping

from catalyst_data.manifests.universe import SourceCell, sha256_identity

SEC_CELL_V2 = "sec_cell_v2"

_S2_EXTENSION_KEYS = frozenset({"cik", "accession_number", "form_policy_version"})
_S4_EXTENSION_KEYS = frozenset(
    {
        "inventory_id",
        "accession_number",
        "document_id",
        "document_role",
        "document_file",
        "document_url",
        "requiredness",
        "requiredness_reason",
        "filing_id",  # enters cell hash; materialize must not accept unvalidated external filing_id
    }
)


def validate_sec_plan_cell_v2(record: Mapping[str, Any]) -> None:
    if record.get("identity_schema_version") != SEC_CELL_V2:
        raise ValueError("identity_schema_version must be sec_cell_v2")
    endpoint = record.get("endpoint_name")
    ext = record.get("identity_extensions")
    if not isinstance(ext, dict) or not ext:
        raise ValueError("identity_extensions required and non-empty for SEC v2 cells")
    if endpoint == "sec_filing_index":
        allowed = _S2_EXTENSION_KEYS
    elif endpoint == "sec_document":
        allowed = _S4_EXTENSION_KEYS
    else:
        raise ValueError(f"unsupported SEC endpoint for v2 cell: {endpoint}")
    keys = set(ext)
    missing = allowed - keys
    unknown = keys - allowed
    if missing:
        raise ValueError(f"missing identity_extensions keys: {sorted(missing)}")
    if unknown:
        raise ValueError(f"unknown identity_extensions keys: {sorted(unknown)}")


def canonical_sec_cell_id(record: Mapping[str, Any]) -> str:
    """Hash of full record excluding cell_id (must match stored cell_id)."""
    body = {k: v for k, v in dict(record).items() if k != "cell_id"}
    return sha256_identity(body)


def validate_sec_plan_cell_v2_with_id(record: Mapping[str, Any]) -> None:
    """Validate extensions and that cell_id matches full-record hash."""
    validate_sec_plan_cell_v2(record)
    stored = record.get("cell_id")
    if not stored or not isinstance(stored, str):
        raise ValueError("cell_id required for sec_cell_v2")
    expected = canonical_sec_cell_id(record)
    if stored != expected:
        raise ValueError(
            f"cell_id mismatch for SEC v2 record: stored={stored[:12]}… "
            f"expected={expected[:12]}…"
        )


def validate_plan_cells(cells: list[Mapping[str, Any]] | list[dict[str, Any]]) -> None:
    """Production planner hook: validate any SEC v2 cells in a plan cell list."""
    for cell in cells:
        if not isinstance(cell, Mapping):
            continue
        if cell.get("identity_schema_version") == SEC_CELL_V2:
            validate_sec_plan_cell_v2_with_id(cell)
        elif cell.get("source_type") == "sec_filings" and cell.get(
            "endpoint_name"
        ) in {"sec_filing_index", "sec_document"}:
            # SEC index/document without v2 schema is invalid for Pre-B6
            raise ValueError(
                "sec_filing_index/sec_document require identity_schema_version=sec_cell_v2"
            )


def build_sec_plan_cell_v2(
    *,
    stage: str,
    source_type: str,
    endpoint_name: str,
    subject: str,
    window_start: str,
    window_end: str,
    date_domain: str,
    provider_profile_version: str,
    identity_extensions: Mapping[str, Any],
    page_cap: int | None = None,
    item_cap: int | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "stage": stage,
        "source_type": source_type,
        "endpoint_name": endpoint_name,
        "subject": subject,
        "window_start": window_start,
        "window_end": window_end,
        "date_domain": date_domain,
        "provider_profile_version": provider_profile_version,
        "page_cap": page_cap,
        "item_cap": item_cap,
        "identity_schema_version": SEC_CELL_V2,
        "identity_extensions": dict(identity_extensions),
    }
    validate_sec_plan_cell_v2(record)
    record["cell_id"] = sha256_identity(
        {k: v for k, v in record.items() if k != "cell_id"}
    )
    return record


def legacy_source_cell_unchanged() -> None:
    """Placeholder documenting that SourceCell.create remains the legacy path."""
    _ = SourceCell
