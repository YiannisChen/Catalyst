from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class CatalystDataRequest(BaseModel):
    ticker: str = Field(..., description="The stock ticker symbol, e.g., 'NVDA'")
    date: str = Field(
        ...,
        description=(
            "Business date for the query, format: YYYY-MM-DD. "
            "Assumed to be US Eastern Time (ET)"
        ),
    )
    sources: List[str] = Field(
        ...,
        description=(
            "List of requested logical sources, "
            "e.g., ['fmp_fundamentals', 'gdelt_news']"
        ),
    )
    force_refresh: bool = Field(
        default=False,
        description=(
            "If True, bypasses the SQLite cache and forces a live network fetch"
        ),
    )

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("ticker must not be empty")
        return v

    @field_validator("date")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"date must be YYYY-MM-DD format, got '{v}'")
        return v

    @field_validator("sources")
    @classmethod
    def validate_sources_not_empty(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("sources must contain at least one entry")
        return v


class DataAsset(BaseModel):
    asset_id: str = Field(
        ...,
        description="Primary Key. SHA256(ticker + date + source_type + data_version).",
    )
    ticker: str = Field(..., description="e.g., 'NVDA'")
    source_type: str = Field(
        ...,
        description="Logical source name, e.g., 'fmp_fundamentals', 'gdelt_news'",
    )

    reference_date_utc: datetime = Field(
        ...,
        description="Physical DB Storage: The absolute UTC time of the event.",
    )
    reference_date_et: str = Field(
        ...,
        description=(
            "Display Format: The human/Agent readable US Eastern Time, "
            "e.g., '2026-01-15 16:05 ET'."
        ),
    )
    last_updated: datetime = Field(
        ...,
        description="The UTC timestamp when this record was fetched/inserted.",
    )

    data_version: str = Field(
        default="v1",
        description="Version of the physical transmuter/cleaner.",
    )
    is_final: bool = Field(
        default=True,
        description=(
            "Whether the data is in its final state "
            "(True for V1 fundamentals and news)."
        ),
    )

    content_raw: Optional[bytes] = Field(
        default=None,
        description=(
            "The raw JSON/HTML. MUST be zlib compressed before SQLite insertion."
        ),
    )
    content_clean: str = Field(
        ...,
        description="The heuristically denoised Markdown (Tables for financials, text for news).",
    )

    metadata: dict = Field(
        default_factory=dict,
        description=(
            "Contains endpoint_statuses, original URLs, latency_ms, etc."
        ),
    )

    @field_validator("metadata")
    @classmethod
    def validate_metadata_json_serializable(cls, v: dict) -> dict:
        try:
            json.dumps(v)
        except (TypeError, ValueError) as e:
            raise ValueError(f"metadata must be JSON-serializable: {e}")
        return v
