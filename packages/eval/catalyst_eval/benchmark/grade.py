"""Grade tooling — constants and helpers for evidence judgments."""
from __future__ import annotations

from typing import Literal

GradeValue = Literal[0, 1, 2]

GRADE_RELEVANT_AND_FAITHFUL: GradeValue = 2
GRADE_RELEVANT_ONLY: GradeValue = 1
GRADE_NOT_RELEVANT: GradeValue = 0

GRADE_LABELS: dict[GradeValue, str] = {
    2: "relevant_and_faithful",
    1: "relevant_only",
    0: "not_relevant",
}

RELEVANT_DIRECT = GRADE_RELEVANT_AND_FAITHFUL
RELEVANT_INDIRECT = GRADE_RELEVANT_ONLY
NOT_RELEVANT = GRADE_NOT_RELEVANT


def is_relevant(grade: GradeValue) -> bool:
    if grade not in (NOT_RELEVANT, RELEVANT_INDIRECT, RELEVANT_DIRECT):
        raise ValueError(f"invalid_grade: {grade}")
    return grade in (RELEVANT_DIRECT, RELEVANT_INDIRECT)


def judgment_for(chunk_id: str, judgments_by_chunk_id: dict):
    """Keep an absent judgment distinct from an explicit grade zero."""
    judgment = judgments_by_chunk_id.get(chunk_id)
    return ("judged", judgment) if judgment is not None else ("unjudged", None)
