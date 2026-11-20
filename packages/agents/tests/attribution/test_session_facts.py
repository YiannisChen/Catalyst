"""Facts panel: code-computed session facts shown with the observed move."""
from datetime import datetime, timezone
from types import SimpleNamespace as NS

from catalyst_agents.attribution.session_facts import _session_facts

UTC = timezone.utc
WINDOW = NS(
    information_window_start_at=datetime(2026, 1, 22, 21, 0, tzinfo=UTC),
    cutoff_at=datetime(2026, 1, 23, 21, 0, tzinfo=UTC),
)


def _align(band, relation, target=-5.0, reference=-1.0):
    return NS(band=NS(value=band), direction_relation=NS(value=relation),
              target_return_pct=target, reference_return_pct=reference)


def _item(url, when):
    return NS(canonical_url=url, eligible_at=when)


def test_comove_verdicts_in_words():
    profile = NS(
        market_comove=_align("DIVERGENT", "OPPOSITE_DIRECTION", reference=0.8),
        sector_comove=_align("ALIGNED", "SAME_DIRECTION", reference=-4.8),
        peer_comove=_align("DIVERGENT", "SAME_DIRECTION"),
    )
    text = _session_facts(profile, NS(evidence_inventory=()), WINDOW)
    assert text == ("Co-movement (computed from closing prices): against the market; "
                    "in line with the sector; same direction as the peers but much larger.")


def test_unknown_bands_and_out_of_window_filings_are_left_out():
    profile = NS(market_comove=_align("UNKNOWN", "REFERENCE_FLAT"), sector_comove=None, peer_comove=None)
    pack = NS(evidence_inventory=(
        _item("https://www.sec.gov/a", datetime(2026, 1, 22, 21, 5, tzinfo=UTC)),
        _item("https://www.sec.gov/a", datetime(2026, 1, 22, 21, 5, tzinfo=UTC)),
        _item("https://www.sec.gov/old", datetime(2026, 1, 20, 12, 0, tzinfo=UTC)),
        _item("https://news.example/x", datetime(2026, 1, 23, 12, 0, tzinfo=UTC)),
    ))
    assert _session_facts(profile, pack, WINDOW) == (
        "SEC filing accepted 2026-01-22 21:05 UTC: https://www.sec.gov/a."
    )


def test_commodity_direction_on_a_one_percent_move():
    profile = NS(market_comove=None, sector_comove=None, peer_comove=None,
                 commodity_ticker="USO", commodity_return=3.45, target_return=-5.25)
    assert _session_facts(profile, NS(evidence_inventory=()), WINDOW) == (
        "Co-movement (computed from closing prices): against the commodity proxy USO."
    )


def test_timing_splits_the_move_into_gap_and_session():
    profile = NS(market_comove=None, sector_comove=None, peer_comove=None,
                 target_return=-17.03, gap_return=-13.73)
    assert _session_facts(profile, NS(evidence_inventory=()), WINDOW) == (
        "Timing: opening gap -13.73%, open to close -3.83%."
    )
