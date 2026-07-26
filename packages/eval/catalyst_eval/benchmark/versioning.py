"""Dataset versioning — semantic version bump rules."""
from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class DatasetVersion(BaseModel):
    """A versioned snapshot of the benchmark dataset."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    parent_version: str | None = None
    change: Literal[
        "metadata_only", "case_added", "case_removed",
        "judgments_modified", "cutoff_modified", "schema_modified",
    ]
    created_at: datetime.datetime

    @field_validator("version", "parent_version")
    @classmethod
    def _semver_or_none(cls, v: str | None) -> str | None:
        import re
        if v is not None and not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", v):
            raise ValueError(f"Not a semantic version: {v}")
        return v

    @field_validator("created_at")
    @classmethod
    def _utc_aware(cls, v: datetime.datetime) -> datetime.datetime:
        if v.tzinfo is None or v.utcoffset() != datetime.timedelta(0):
            raise ValueError("created_at must use UTC offset +00:00")
        return v


def bump_version(version: str, change: str) -> str:
    """Bump a semantic version according to the change type.

    metadata_only -> patch +1
    case_added, case_removed -> minor +1, patch=0
    judgments_modified, cutoff_modified, schema_modified -> major +1, minor=patch=0
    """
    parts = version.split(".")
    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])

    if change == "metadata_only":
        patch += 1
    elif change in ("case_added", "case_removed"):
        minor += 1
        patch = 0
    elif change in ("judgments_modified", "cutoff_modified", "schema_modified"):
        major += 1
        minor = 0
        patch = 0
    else:
        raise ValueError(f"Unknown change type: {change}")

    return f"{major}.{minor}.{patch}"
