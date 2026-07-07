"""Tests for fallback.py — H3 FallbackPolicy."""

from catalyst_data.fallback import FallbackPolicy


class TestFallbackPolicy:
    def test_chain_order(self):
        fp = FallbackPolicy()
        assert fp.next_provider("polygon_news") == "finnhub_company_news"

    def test_excludes_auth(self):
        fp = FallbackPolicy()
        assert fp.should_attempt("auth", 0) is False

    def test_excludes_rate_limit(self):
        fp = FallbackPolicy()
        assert fp.should_attempt("rate_limit", 0) is False

    def test_excludes_budget(self):
        fp = FallbackPolicy()
        assert fp.should_attempt("budget_exhausted", 0) is False

    def test_allows_timeout(self):
        fp = FallbackPolicy()
        assert fp.should_attempt("timeout", 0) is True

    def test_allows_provider_5xx(self):
        fp = FallbackPolicy()
        assert fp.should_attempt("provider_5xx", 0) is True

    def test_depth_exhausted(self):
        fp = FallbackPolicy()
        assert fp.next_provider("finnhub_company_news") is None

    def test_max_depth_gate(self):
        fp = FallbackPolicy(max_fallback_depth=0)
        assert fp.should_attempt("timeout", 0) is False

    def test_sec_excluded(self):
        fp = FallbackPolicy()
        assert "sec_filings" not in fp.chain
        assert "fred_macro" not in fp.chain
