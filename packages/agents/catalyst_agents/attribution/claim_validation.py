"""V1.1 ClaimValidator failure taxonomy (M2-7).

Schema and failure classes only (Final Migration TSD §13). Attribution-support
failure drops/downgrades and may yield ABSTAIN; integrity/system failure fails
the run and never becomes ABSTAIN.
"""
from __future__ import annotations

from enum import Enum


class ClaimValidatorFailureType(str, Enum):
    ATTRIBUTION_SUPPORT = "ATTRIBUTION_SUPPORT"
    INTEGRITY_SYSTEM = "INTEGRITY_SYSTEM"


class ClaimValidatorFailure(Exception):
    """Base ClaimValidator failure carrying a typed failure class."""

    failure_type: ClaimValidatorFailureType

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(f"{self.failure_type.value}: {code}" + (f": {message}" if message else ""))


class AttributionSupportFailure(ClaimValidatorFailure):
    """Insufficient material support/independence; drops or downgrades, may ABSTAIN."""

    failure_type = ClaimValidatorFailureType.ATTRIBUTION_SUPPORT


class IntegritySystemFailure(ClaimValidatorFailure):
    """Missing evidence ID, cross-run evidence, schema invariant; fails the run."""

    failure_type = ClaimValidatorFailureType.INTEGRITY_SYSTEM


__all__ = [
    "AttributionSupportFailure",
    "ClaimValidatorFailure",
    "ClaimValidatorFailureType",
    "IntegritySystemFailure",
]
