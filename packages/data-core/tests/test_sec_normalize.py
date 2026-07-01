import json
import pytest
from pathlib import Path

from catalyst_data.pipeline.sec_normalize import (
    normalize_submissions,
    _is_relevant_8k,
    _parse_ex99_from_index,
    extract_text_from_html,
)
from catalyst_data.connectors.sec import create_sec_fetcher

FIXTURES = Path(__file__).parent / "fixtures"


def _load_submissions():
    with open(FIXTURES / "sec_submissions_AAPL.json") as f:
        return json.load(f)


class TestNormalizeSubmissions:
    def test_filters_date_range(self):
        data = _load_submissions()
        result = normalize_submissions(data, "AAPL", "0000320193", "2026-04-01", "2026-05-31")
        # Should include 8-K from 2025-05-01 and 10-Q from 2025-05-01
        forms = [r["form_type"] for r in result]
        assert "8-K" in forms or "10-Q" in forms
        filed_dates = [r["filed_at"] for r in result]
        for fd in filed_dates:
            assert "2026-04-01" <= fd <= "2026-05-31"

    def test_filters_allowed_forms_only(self):
        data = _load_submissions()
        result = normalize_submissions(data, "AAPL", "0000320193", "2024-01-01", "2026-12-31")
        forms = {r["form_type"] for r in result}
        assert forms.issubset({"8-K", "10-Q", "10-K"})

    def test_sets_source_tier_1(self):
        data = _load_submissions()
        result = normalize_submissions(data, "AAPL", "0000320193", "2024-01-01", "2026-12-31")
        for r in result:
            assert r["source_tier"] == 1

    def test_empty_window(self):
        data = _load_submissions()
        result = normalize_submissions(data, "AAPL", "0000320193", "2010-01-01", "2010-01-02")
        assert result == []

    def test_filing_id_format(self):
        data = _load_submissions()
        result = normalize_submissions(data, "AAPL", "0000320193", "2024-01-01", "2026-12-31")
        for r in result:
            assert r["filing_id"].startswith("sec:")
            assert ":" in r["filing_id"]
            assert r["dedup_group_id"] is not None
            assert len(r["dedup_group_id"]) == 16


class Test8KItemFiltering:
    def test_relevant_items(self):
        assert _is_relevant_8k("2.02,9.01") is True
        assert _is_relevant_8k("1.01") is True
        assert _is_relevant_8k("5.02") is True

    def test_non_relevant_items(self):
        assert _is_relevant_8k("3.01") is False
        assert _is_relevant_8k("3.02,5.03") is False

    def test_empty_items(self):
        assert _is_relevant_8k(None) is False
        assert _is_relevant_8k("") is False

    def test_relevant_8k_gets_rag_eligible(self):
        data = _load_submissions()
        result = normalize_submissions(data, "AAPL", "0000320193", "2025-04-01", "2025-06-01")
        eights = [r for r in result if r["form_type"] == "8-K"]
        for r in eights:
            # Should have is_rag_eligible based on items
            items = json.loads(r["items_json"])
            has_relevant = any(i in {"1.01","1.03","2.02","2.05","2.06","5.02","7.01","8.01","9.01"} for i in items)
            assert r["is_rag_eligible"] == (1 if has_relevant else 0)


class TestExhibitParsing:
    def test_parse_ex99_from_index_escaped(self):
        html = "<HTML><PRE>&lt;TYPE&gt;EX-99.1\n&lt;FILENAME&gt;exhibit99_1.htm\n&lt;DESCRIPTION&gt;EX-99.1</PRE></HTML>"
        result = _parse_ex99_from_index(html)
        assert "exhibit99_1.htm" in result

    def test_parse_ex99_from_real_fixture(self):
        with open(FIXTURES / "sec_8k_index_headers.htm", "r", encoding="latin-1", errors="replace") as f:
            html = f.read()
        result = _parse_ex99_from_index(html)
        assert len(result) >= 1


class TestTextExtraction:
    def test_extract_text_from_real_exhibit(self):
        with open(FIXTURES / "sec_8k_exhibit_99_1.htm", "rb") as f:
            html = f.read().decode("latin-1", errors="replace")
        text = extract_text_from_html(html)
        assert len(text) > 200
        assert "Apple" in text or "Inc" in text

    def test_extract_text_matches_expected(self):
        with open(FIXTURES / "sec_8k_exhibit_99_1.txt") as f:
            expected = f.read()
        with open(FIXTURES / "sec_8k_exhibit_99_1.htm", "rb") as f:
            html = f.read().decode("latin-1", errors="replace")
        text = extract_text_from_html(html)
        assert len(text) > 200
        assert len(text) >= len(expected) * 0.5  # At least 50% of expected extraction

    def test_empty_html(self):
        assert extract_text_from_html("") == ""
        assert extract_text_from_html("<html></html>") == ""


class TestTenQTKMetadataOnly:
    def test_10q_not_8k_returns_no_docs(self):
        """10-Q/10-K should not trigger document fetch in resolve_filing_documents."""
        filing = {
            "filing_id": "sec:0000320193:TEST",
            "form_type": "10-Q",
            "is_rag_eligible": 1,
        }
        import asyncio
        result = asyncio.run(_resolve_empty(filing))
        assert result == []


async def _resolve_empty(filing):
    from catalyst_data.pipeline.sec_normalize import resolve_filing_documents
    from types import SimpleNamespace
    fetcher = SimpleNamespace(fetch_document=lambda url: None)
    return await resolve_filing_documents(fetcher, filing)
