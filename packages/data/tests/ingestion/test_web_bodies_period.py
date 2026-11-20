"""R6 extension: a price move over another month is a recap, not a cause."""
from catalyst_data.retrieval.web_bodies import other_period_move


def test_price_move_in_another_month_is_a_recap():
    assert other_period_move("With Brent Crude prices up around 17% in January, it is hardly surprising.", "2026-02-23")
    assert other_period_move("This helps explain why the January oil rally led to a price advance.", "2026-02-23")


def test_same_month_and_statistics_are_not_recaps():
    assert not other_period_move("Oil prices rose 4% in February trading.", "2026-02-23")
    assert not other_period_move("The May Consumer Price Index rose 0.5% on the month.", "2026-06-10")
    assert not other_period_move("May nonfarm payrolls rose 172,000, the best number since March.", "2026-06-05")
    assert not other_period_move("Spot gold was down 0.7%, after hitting its highest since March 18.", "2026-04-15")
    assert not other_period_move("A broad selloff began with the June's hawkish dot plot.", "2026-06-17")
