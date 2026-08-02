"""Eval compatibility exports for production-owned Pre-B6 coverage gates."""

from catalyst_data.pre_b6_probes import (
    CoverageResult,
    check_corpus_coverage_invariant,
)

__all__ = ["CoverageResult", "check_corpus_coverage_invariant"]
