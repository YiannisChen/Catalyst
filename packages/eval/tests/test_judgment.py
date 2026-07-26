from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from catalyst_eval.benchmark.judgment import EvidenceJudgment


def test_judged_at_must_be_utc_not_merely_timezone_aware():
    with pytest.raises(ValidationError):
        EvidenceJudgment(
            chunk_id="polygon:a:news_v2:body:0001", grade=2,
            rationale="Direct evidence.", annotator="reviewer",
            judged_at=datetime(2026, 1, 20, 12, tzinfo=timezone(timedelta(hours=2))),
        )
