"""A story published before the open cannot explain an intraday move."""
from datetime import datetime, timezone
from types import SimpleNamespace as NS

from catalyst_agents.attribution.analyst import AttributionStatus, HypothesisRole
from catalyst_agents.attribution.assessment import _timing_ceiling

UTC = timezone.utc
OPEN = datetime(2026, 5, 11, 13, 30, tzinfo=UTC)


def _check(gap, total, *times):
    ids = tuple(f"e{i}" for i in range(len(times)))
    hypothesis = NS(normalized_role=HypothesisRole.PRIMARY, cause_type=NS(value="ANALYST_ACTION"),
                    supporting_evidence_ids=ids)
    pack = NS(
        observation=NS(gap_return=gap, target_return=total),
        temporal_identity=NS(session_open_at=OPEN),
        evidence_inventory=tuple(NS(evidence_id=i, eligible_at=t) for i, t in zip(ids, times)),
    )
    return _timing_ceiling((hypothesis,), pack)


PRE = datetime(2026, 5, 11, 9, 0, tzinfo=UTC)
DURING = datetime(2026, 5, 11, 16, 38, tzinfo=UTC)


def test_pre_open_story_with_an_intraday_move_abstains():
    assert _check(0.24, -5.44, PRE) == (
        AttributionStatus.ABSTAIN, "primary_pre_open_intraday_move:ANALYST_ACTION")


def test_gap_move_or_in_session_source_or_small_day_is_left_alone():
    assert _check(-13.7, -17.0, PRE) == (None, "")      # priced at the open
    assert _check(0.24, -5.44, PRE, DURING) == (None, "")  # a source from the session
    assert _check(0.1, -1.5, PRE) == (None, "")         # under 2%
