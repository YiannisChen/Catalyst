import pytest

from catalyst_eval.benchmark.grade import (
    NOT_RELEVANT,
    RELEVANT_DIRECT,
    RELEVANT_INDIRECT,
    judgment_for,
    is_relevant,
)


def test_grade_helpers_keep_unjudged_distinct_from_zero():
    assert (RELEVANT_DIRECT, RELEVANT_INDIRECT, NOT_RELEVANT) == (2, 1, 0)
    assert is_relevant(2) and is_relevant(1) and not is_relevant(0)
    assert judgment_for("missing", {}) == ("unjudged", None)


def test_is_relevant_rejects_out_of_contract_grade():
    with pytest.raises(ValueError, match="invalid_grade"):
        is_relevant(3)
