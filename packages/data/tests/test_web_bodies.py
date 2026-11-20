"""M9-1 attached news-body store."""
from __future__ import annotations

import pytest
from catalyst_data.retrieval import web_bodies
from catalyst_data.storage.errors import StoreReadError


def _row(asset_id: str, text: str) -> dict:
    return {
        "asset_id": asset_id, "text": text, "url": f"https://example.com/{asset_id}",
        "title": "Title", "publisher": "Example", "source_class": "reported_news",
        "eligible_at": "2026-01-27T15:00:00Z", "fetched_at": "2026-09-30T00:00:00Z",
        "body_sha256": "a" * 64,
    }


def test_chunks_are_paragraph_bounded_and_size_capped():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(6))
    chunks = web_bodies.chunk_text(text, limit=700)
    assert len(chunks) > 1
    assert all(chunk.startswith("Paragraph") for chunk in chunks)
    assert all(len(chunk.encode()) <= 700 for chunk in chunks)


def test_store_serves_at_most_two_chunks_per_asset_only_when_enabled(tmp_path, monkeypatch):
    store = tmp_path / "bodies.sqlite3"
    text = "\n\n".join("Sentence number %d. " % i * 30 for i in range(5))
    assert web_bodies.build_store(store, [_row("asset:1", text)]) >= 3
    monkeypatch.delenv(web_bodies.ENV_VAR, raising=False)
    assert web_bodies.bodies_for_assets(["asset:1"]) == {}
    monkeypatch.setenv(web_bodies.ENV_VAR, str(store))
    served = web_bodies.bodies_for_assets(["asset:1", "asset:missing"])
    assert list(served) == ["asset:1"]
    assert [c["ordinal"] for c in served["asset:1"]] == [1, 2]
    assert served["asset:1"][0]["chunk_id"] == "asset:1:web_body:0001"
    assert served["asset:1"][0]["capture_mode"] == "LIVE_REFETCH"


def test_configured_missing_body_store_is_not_an_empty_result(tmp_path, monkeypatch):
    monkeypatch.setenv(web_bodies.ENV_VAR, str(tmp_path / "missing.sqlite3"))

    with pytest.raises(StoreReadError, match="web body store"):
        web_bodies.bodies_for_assets(["asset:missing"])


def test_publisher_comes_from_the_serving_host_not_the_feed(tmp_path, monkeypatch):
    store = tmp_path / "bodies.sqlite3"
    row = {**_row("asset:2", "One paragraph of body text. " * 20),
           "url": "https://stocktwits.com/news-articles/x", "publisher": "Yahoo"}
    shell = {**_row("asset:3", "Finnhub shell page text. " * 20),
             "url": "https://finnhub.io/api/news?id=1"}
    web_bodies.build_store(store, [row, shell])
    monkeypatch.setenv(web_bodies.ENV_VAR, str(store))
    served = web_bodies.bodies_for_assets(["asset:2", "asset:3"])
    assert served["asset:2"][0]["publisher"] == "Stocktwits"
    assert "asset:3" not in served
