"""Inactive V1.1 generation staging (M3-10) and promotion (M3-12A)."""

from catalyst_data.index.v1_promote import (
    PromotionResult,
    promote_v1_generation,
    rollback_v1_generation,
)
from catalyst_data.index.v1_staging import (
    InactiveDenseCandidate,
    stage_dense,
    validate_dense_candidate,
)

__all__ = [
    "InactiveDenseCandidate",
    "PromotionResult",
    "promote_v1_generation",
    "rollback_v1_generation",
    "stage_dense",
    "validate_dense_candidate",
]
