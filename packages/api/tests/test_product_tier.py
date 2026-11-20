from catalyst_app.product_tier import group_context, product_tier, with_product_fields


def _facts(stock, peers=None, sector=None):
    facts = {"target_return_pct": stock}
    if peers is not None:
        facts["peers"] = {"median_return_pct": peers, "count": 4}
    if sector is not None:
        facts["sector"] = {"return_pct": sector, "ticker": "XLK"}
    return facts


def test_relations():
    assert group_context(_facts(-4.4, peers=-3.6))["relation"] == "in_line"
    assert group_context(_facts(0.06, peers=4.71))["relation"] == "smaller"
    assert group_context(_facts(-8.0, peers=-1.8))["relation"] == "larger"
    assert group_context(_facts(2.6, peers=-0.8))["relation"] == "against"
    assert group_context(_facts(3.0, peers=0.1))["relation"] == "flat"
    assert group_context(_facts(-3.0, peers=-2.5, sector=1.2))["relation"] == "disagree"
    assert group_context(_facts(-3.0, sector=-2.6))["relation"] == "in_line"
    assert group_context({})["relation"] == "unknown"


def test_tiers():
    assert product_tier("ANSWERED", {"relation": "against"}) == "CONFIRMED"
    assert product_tier("TIER_ONLY", {"relation": "unknown"}) == "ORDINARY_MOVE"
    assert product_tier("ABSTAINED", {"relation": "in_line"}) == "MOVED_WITH_GROUP"
    assert product_tier("ABSTAINED", {"relation": "smaller"}) == "MOVED_WITH_GROUP"
    assert product_tier("ABSTAINED", {"relation": "larger"}) == "LARGER_THAN_GROUP"
    assert product_tier("ABSTAINED", {"relation": "against"}) == "NOTHING_FOUND"
    assert product_tier("ABSTAINED", {"relation": "against", "reported_reason": {"quote": "x"}}) == "REPORTED"


def test_leads_after_the_close_are_dropped_and_input_is_not_mutated():
    snapshot = {
        "outcome": "ABSTAINED",
        "facts": _facts(-3.86, peers=-1.2),
        "window": {"cutoff_at": "2026-04-28T20:00:00+00:00"},
        "abstention": {"leads": [
            {"title": "before", "published_at": "2026-04-28 14:00 UTC"},
            {"title": "after", "published_at": "2026-04-29 14:43 UTC"},
            {"title": "undated"},
        ]},
    }
    answer = with_product_fields(snapshot)
    assert [lead["title"] for lead in answer["abstention"]["leads"]] == ["before", "undated"]
    assert answer["product_tier"] == "LARGER_THAN_GROUP"
    assert len(snapshot["abstention"]["leads"]) == 3
    assert "product_tier" not in snapshot
