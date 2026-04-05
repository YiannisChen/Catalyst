from catalyst_eval.schema.golden_event import GoldenEvent, Cause, CauseCategory
from catalyst_eval.schema.result import AttributionResult, PredictedCause


def test_golden_event_validates():
    event = GoldenEvent(
        id="test_001", ticker="AAPL", trade_date="2026-01-15",
        price_move_pct=-4.2,
        causes=[Cause(text="Earnings miss", category=CauseCategory.EARNINGS,
                       weight=0.7, temporal_anchor="after-hours", evidence_ids=["a1"])]
    )
    assert event.ticker == "AAPL"
    assert event.causes[0].weight == 0.7


def test_golden_event_rejects_invalid_weight():
    import pytest
    with pytest.raises(Exception):
        Cause(text="Bad", category=CauseCategory.EARNINGS,
              weight=1.5, temporal_anchor="intraday", evidence_ids=[])


def test_attribution_result_validates():
    result = AttributionResult(
        ticker="AAPL", trade_date="2026-01-15",
        causes=[PredictedCause(text="Earnings miss", category="earnings",
                                confidence=0.8, evidence_ids=["c1"], direction="negative")],
        summary="AAPL dropped due to earnings miss [c1].",
        retrieved_chunks=["chunk1"],
        cost_breakdown=[], total_cost_usd=0.03, total_tokens=7500
    )
    assert result.causes[0].confidence == 0.8


def test_attribution_result_defaults():
    result = AttributionResult(
        ticker="AAPL", trade_date="2026-01-15",
        causes=[], summary="No info.",
    )
    assert result.total_cost_usd == 0.0
    assert result.retrieved_chunks == []
