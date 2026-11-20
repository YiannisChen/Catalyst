"""Registry-backed readiness mapping; no provider-only target/peer inference."""
import copy

import pytest
from catalyst_data.ingestion import source_categories as C
from catalyst_data.ingestion import sources

PUBLIC = {"price_bars", "sec_filings", "target_news", "peer_news", "web_search", "supplementary_news"}


def test_public_category_contract_and_exhaustive_registry_membership():
    mapping = C.load()
    assert set(C.CATEGORIES) == PUBLIC
    assert set(mapping["categories"]) == PUBLIC
    assert set(mapping["sources"]) == {s["id"] for s in sources.load()["sources"]}
    C.validate(mapping, sources.load())


@pytest.mark.parametrize("source_id,expected", [
    ("daily:sec", "sec_filings"),
    ("daily:discovery_v2", "web_search"),
    ("daily:discovery", "web_search"),
    ("daily:kitco", "supplementary_news"),
])
def test_fixed_sources_have_fixed_public_category(source_id, expected):
    assert C.category_for(source_id) == expected


@pytest.mark.parametrize("source_id", ["alpaca_news:news:poll", "alpaca_news:backfill",
                                       "daily:finnhub+polygon"])
def test_multirole_news_requires_explicit_request_scope(source_id):
    with pytest.raises(ValueError, match="scope"):
        C.category_for(source_id)
    assert C.category_for(source_id, scope="target") == "target_news"
    assert C.category_for(source_id, scope="peer") == "peer_news"
    assert C.category_for(source_id, scope="supplementary") == "supplementary_news"


@pytest.mark.parametrize("source_id,scope", [("alpaca", "target"),
                                             ("alpaca_news:news:poll", "unknown"),
                                             ("daily:sec", "unknown")])
def test_unknown_source_or_scope_fails_closed(source_id, scope):
    with pytest.raises(ValueError):
        C.category_for(source_id, scope=scope)


@pytest.mark.parametrize("change", ["missing", "extra", "legacy_category", "scope_inference", "fixed_news_inference", "bad_scope", "unknown_field"])
def test_invalid_mapping_cannot_validate(change):
    mapping = copy.deepcopy(C.load())
    if change == "missing":
        del mapping["sources"]["daily:sec"]
    elif change == "extra":
        mapping["sources"]["unknown:provider"] = {"category": "price_bars"}
    elif change == "legacy_category":
        mapping["sources"]["daily:kitco"] = {"category": "benzinga_kitco"}
    elif change == "scope_inference":
        mapping["sources"]["alpaca_news:news:poll"] = {"by_scope": {"target": "target_news"}}
    elif change == "fixed_news_inference":
        mapping["sources"]["alpaca_news:news:poll"] = {"category": "target_news"}
    elif change == "bad_scope":
        mapping["sources"]["alpaca_news:news:poll"]["by_scope"]["unknown"] = "peer_news"
    else:
        mapping["sources"]["daily:sec"]["fallback"] = "target_news"
    with pytest.raises(ValueError):
        C.validate(mapping, sources.load())
