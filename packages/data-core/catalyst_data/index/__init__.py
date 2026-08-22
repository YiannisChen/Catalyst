"""Inactive V1.1 generation staging (M3-10). Promotion APIs arrive in M3-12A."""

from catalyst_data.index.v1_staging import (
    InactiveDenseCandidate,
    stage_dense,
    validate_dense_candidate,
)

__all__ = [
    "InactiveDenseCandidate",
    "stage_dense",
    "validate_dense_candidate",
]
