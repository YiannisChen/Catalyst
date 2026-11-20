"""A run reads SEC from the local cache only."""
from catalyst_agents.runtime.earnings_timing import run_earnings_timing_scope
from catalyst_data.ingestion.collectors import sec_filings


def test_run_scope_turns_on_cached_only_sec_reads():
    assert sec_filings._CACHED_ONLY.get() is False
    with run_earnings_timing_scope():
        assert sec_filings._CACHED_ONLY.get() is True
    assert sec_filings._CACHED_ONLY.get() is False
