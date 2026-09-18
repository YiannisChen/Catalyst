"""Catalyst V1.1 eval contracts (eval owned).

Public surfaces use professional benchmark terminology (M7 completion
operator guide §2): ``BenchmarkCase`` (``is GoldenCase``),
``BenchmarkDatasetManifest``, ``HumanOutputAudit`` (``is Stage1OutputAudit``).
The sealed legacy names stay exported so historical V1/V1.2 artifacts and
adapters keep validating; they are never rewritten.
"""

from catalyst_eval.v1_1.case import BenchmarkCase, GoldenCase
from catalyst_eval.v1_1.loader import (
    BenchmarkDatasetManifest,
    load_benchmark_cases,
    load_golden_cases,
)
from catalyst_eval.v1_1.manifest import EligibleExperiment, EvalManifest, RunManifestBinding
from catalyst_eval.v1_1.output_audit import HumanOutputAudit, Stage1OutputAudit

__all__ = [
    "BenchmarkCase",
    "BenchmarkDatasetManifest",
    "EligibleExperiment",
    "EvalManifest",
    "GoldenCase",
    "HumanOutputAudit",
    "RunManifestBinding",
    "Stage1OutputAudit",
    "load_benchmark_cases",
    "load_golden_cases",
]
