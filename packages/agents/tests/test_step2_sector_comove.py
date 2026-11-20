"""Step 2: sector ETF in the SECTOR_NEWS scope; co-movement ceiling on a
sector- or market-driven PRIMARY."""
from types import SimpleNamespace as NS

from catalyst_agents.attribution.analyst import AttributionStatus
from catalyst_agents.attribution.assessment import _comove_ceiling
from catalyst_agents.attribution.move_profile import AlignmentBand
from catalyst_agents.retrieval import policy


def _band(band, relation="SAME_DIRECTION", ref=2.0):
    return NS(band=AlignmentBand(band), direction_relation=NS(value=relation), reference_return_pct=ref)


def _hyp(cause):
    from catalyst_agents.attribution.analyst import HypothesisRole
    return NS(normalized_role=HypothesisRole.PRIMARY, cause_type=NS(value=cause))


def _obs(**kw):
    base = dict(market_comove=None, sector_comove=None, peer_comove=None,
                commodity_return=None, target_return=3.0)
    base.update(kw)
    return NS(**base)


def test_sector_primary_with_aligned_sector_passes():
    assert _comove_ceiling((_hyp("SECTOR_MOVE"),), _obs(sector_comove=_band("ALIGNED"))) == (None, "")


def test_sector_primary_against_opposite_sector_and_peers_abstains():
    obs = _obs(sector_comove=_band("DIVERGENT", "OPPOSITE_DIRECTION", -1.9),
               peer_comove=_band("DIVERGENT", "OPPOSITE_DIRECTION", -1.0))
    assert _comove_ceiling((_hyp("SECTOR_MOVE"),), obs) == (AttributionStatus.ABSTAIN, "comove_opposite:SECTOR_MOVE")


def test_sector_primary_diverging_same_way_passes():
    obs = _obs(sector_comove=_band("DIVERGENT", "SAME_DIRECTION", 0.6))
    assert _comove_ceiling((_hyp("SECTOR_MOVE"),), obs) == (None, "")


def test_sector_primary_mixed_divergence_passes():
    obs = _obs(sector_comove=_band("DIVERGENT", "OPPOSITE_DIRECTION", -1.9),
               peer_comove=_band("DIVERGENT", "SAME_DIRECTION", 0.4))
    assert _comove_ceiling((_hyp("SECTOR_MOVE"),), obs) == (None, "")


def test_macro_primary_passes_on_aligned_commodity_and_unknown_references():
    obs = _obs(market_comove=_band("DIVERGENT", "SAME_DIRECTION", 0.3), commodity_return=2.0)
    assert _comove_ceiling((_hyp("MACRO_EVENT"),), obs) == (None, "")
    assert _comove_ceiling((_hyp("MACRO_EVENT"),), _obs()) == (None, "")


def test_company_primary_is_untouched():
    obs = _obs(sector_comove=_band("DIVERGENT", "OPPOSITE_DIRECTION", -2.0))
    assert _comove_ceiling((_hyp("COMPANY_SPECIFIC_CATALYST"),), obs) == (None, "")


def test_sector_scope_adds_etf_only_on_material_aligned_sector_move():
    peers = NS(peer_returns=(NS(ticker="NEE"), NS(ticker="DUK")))
    aligned = NS(peer_summary=peers, sector_ticker="XLU", sector_comove=_band("ALIGNED", ref=2.2))
    assert policy._sector_scope(aligned) == ("DUK", "NEE", "XLU")
    small = NS(peer_summary=peers, sector_ticker="XLU", sector_comove=_band("ALIGNED", ref=0.4))
    assert policy._sector_scope(small) == ("DUK", "NEE")
    no_peers = NS(peer_summary=None, sector_ticker="XLU", sector_comove=_band("ALIGNED", ref=2.2))
    assert policy._sector_scope(no_peers) == ("XLU",)


def test_sector_terms_match_group_phrases_not_single_companies():
    from catalyst_data.retrieval.web_bodies import names_subject
    assert names_subject("XLU", "Stocks mixed after jobs data", "Utilities led gains as yields fell.")
    assert names_subject("SMH", "Chip stocks slide as Nvidia falls", "")
    assert not names_subject("XLV", "Merck wins approval for new drug", "Merck said the FDA approved it.")
