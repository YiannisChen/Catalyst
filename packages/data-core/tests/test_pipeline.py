from __future__ import annotations

from catalyst_data.pipeline.stages import StageResult
from catalyst_data.pipeline.ingest import run_ingest
from catalyst_data.pipeline.clean import run_clean
from catalyst_data.pipeline.transform import run_transform


def test_stage_result_ok():
    r = StageResult(stage="ingest", ok=True, data={"test": 1})
    assert r.ok
    assert r.data == {"test": 1}
    assert r.error is None


def test_stage_result_error():
    r = StageResult(stage="ingest", ok=False, error="boom")
    assert not r.ok


def test_ingest_rejects_empty_data():
    result = run_ingest(endpoint_data={}, endpoint_statuses={"news": 500})
    assert not result.ok
    assert "No endpoint returned data" in result.error


def test_ingest_accepts_partial_data():
    result = run_ingest(
        endpoint_data={"news": [{"title": "Test"}]},
        endpoint_statuses={"news": 200, "ohlcv": 500},
    )
    assert result.ok


def test_ingest_accepts_empty_success_payload():
    result = run_ingest(
        endpoint_data={"news": {"results": []}},
        endpoint_statuses={"news": 200},
    )
    assert result.ok
    assert result.data == {"news": {"results": []}}


def test_clean_deduplicates_news_by_title():
    raw = {
        "articles": [
            {"title": "Apple Beats Earnings", "published_utc": "2026-01-15T10:00:00Z"},
            {"title": "Apple beats earnings!", "published_utc": "2026-01-15T10:30:00Z"},
            {"title": "Different Story", "published_utc": "2026-01-15T11:00:00Z"},
        ]
    }
    result = run_clean(raw, source_type="polygon_news")
    assert result.ok
    assert len(result.data) == 2  # deduped by normalized title + time window


def test_clean_merges_financial_endpoints():
    raw = {
        "income_statement": [{"revenue": 100}],
        "balance_sheet": [{"assets": 200}],
    }
    result = run_clean(raw, source_type="fmp_fundamentals")
    assert result.ok
    assert "income_statement" in result.data
    assert "balance_sheet" in result.data


def test_transform_news_to_markdown():
    articles = [
        {
            "title": "Apple Beats Earnings",
            "source": "Reuters",
            "published_utc": "2026-01-15T10:00:00Z",
            "url": "https://example.com",
            "content": "Apple reported record revenue.",
        }
    ]
    result = run_transform(articles, source_type="polygon_news", ticker="AAPL")
    assert result.ok
    assert "Apple Beats Earnings" in result.data
    assert "*Source:" in result.data


def test_transform_polygon_news_uses_live_field_names():
    articles = [
        {
            "title": "Apple Beats Earnings",
            "publisher": {"name": "Reuters"},
            "published_utc": "2026-01-15T10:00:00Z",
            "article_url": "https://example.com/article",
            "description": "Apple reported record revenue.",
        }
    ]
    result = run_transform(articles, source_type="polygon_news", ticker="AAPL")
    assert result.ok
    assert "Reuters" in result.data
    assert "https://example.com/article" in result.data


def test_transform_news_tolerates_string_publisher_shape():
    articles = [
        {
            "published_utc": "2026-01-15T10:00:00Z",
            "publisher": "Reuters",
            "description": "Shape drifted but content should still render.",
        }
    ]
    result = run_transform(articles, source_type="polygon_news", ticker="AAPL")
    assert result.ok
    assert "Untitled" in result.data
    assert "Reuters" in result.data
    assert "Shape drifted but content should still render." in result.data


def test_transform_financial_to_markdown_table():
    data = {"income_statement": {"revenue": 100, "net_income": 50}}
    result = run_transform(data, source_type="fmp_fundamentals", ticker="AAPL")
    assert result.ok
    assert "|" in result.data  # contains markdown table
