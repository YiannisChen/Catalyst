import pytest
from catalyst_data.cik_map import ticker_to_cik, cik_to_ticker, SUPPORTED_TICKERS


class TestCikMap:
    def test_all_10_tickers_resolve(self):
        for t in sorted(SUPPORTED_TICKERS):
            cik = ticker_to_cik(t)
            assert len(cik) == 10, f"{t}: CIK not 10-char padded: {cik}"
            assert cik.isdigit()

    def test_ticker_to_cik_aapl(self):
        assert ticker_to_cik("AAPL") == "0000320193"

    def test_cik_to_ticker_aapl(self):
        assert cik_to_ticker("0000320193") == "AAPL"

    def test_missing_ticker_raises(self):
        with pytest.raises(KeyError, match="ZZZZ"):
            ticker_to_cik("ZZZZ")

    def test_lowercase_ticker_works(self):
        assert ticker_to_cik("aapl") == "0000320193"
