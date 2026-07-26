"""B4 Eval Foundation — schemas for benchmarks, judgments, pools, lineage."""
from catalyst_eval.benchmark.case import BenchmarkCase, CauseLabel
from catalyst_eval.benchmark.judgment import EvidenceJudgment
from catalyst_eval.benchmark.pool_manifest import PoolManifest, PoolArm
from catalyst_eval.benchmark.lineage import LineageRecord, ActionTimestamps, validate_lineage
from catalyst_eval.benchmark.versioning import DatasetVersion, bump_version
from catalyst_eval.benchmark.grade import (
    GRADE_RELEVANT_AND_FAITHFUL,
    GRADE_RELEVANT_ONLY,
    GRADE_NOT_RELEVANT,
    GRADE_LABELS,
    RELEVANT_DIRECT,
    RELEVANT_INDIRECT,
    NOT_RELEVANT,
    is_relevant,
    judgment_for,
)

__all__ = [
    "ActionTimestamps", "BenchmarkCase", "CauseLabel", "DatasetVersion",
    "EvidenceJudgment", "LineageRecord", "PoolArm", "PoolManifest",
    "RELEVANT_DIRECT", "RELEVANT_INDIRECT", "NOT_RELEVANT", "bump_version",
    "is_relevant", "judgment_for", "validate_lineage",
]
