from __future__ import annotations

import json
import sqlite3

from catalyst_app.routers.sessions import router
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_session_news_route_contract_uses_persisted_run_evidence(tmp_path, monkeypatch):
    from catalyst_data.ingestion import media

    bodies = tmp_path / "bodies.sqlite3"
    archive = tmp_path / "archive.sqlite3"
    runtime = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(bodies) as conn:
        conn.executescript(
            """
            CREATE TABLE web_body_chunks (
                asset_id TEXT, ordinal INTEGER, chunk_id TEXT, text TEXT,
                canonical_url TEXT, title TEXT, publisher TEXT, eligible_at TEXT
            );
            CREATE TABLE web_headlines (asset_id TEXT, ticker TEXT, eligible_at TEXT);
            CREATE TABLE web_asset_media (
                asset_id TEXT, canonical_url TEXT, image_url TEXT, image_source TEXT, checked_at TEXT
            );
            INSERT INTO web_body_chunks VALUES
              ('web:api','1','web:api:web_body:0001','Acme quarterly earnings beat',
               'https://example.com/a','Acme reports earnings','Example','2026-01-06T15:00:00Z');
            INSERT INTO web_headlines VALUES ('web:api','ACME','2026-01-06T15:00:00Z');
            """
        )
    (tmp_path / "sec_company_tickers.json").write_text(
        json.dumps({"0": {"ticker": "ACME", "title": "Acme Corporation"}})
    )
    with sqlite3.connect(archive) as conn:
        conn.executescript(
            """
            CREATE TABLE finnhub_news (
                symbol TEXT, news_id INTEGER, published_utc TEXT, headline TEXT,
                url TEXT, source TEXT, summary TEXT, raw_json TEXT
            );
            CREATE TABLE polygon_feed (
                session_date TEXT, news_id TEXT, published_utc TEXT, title TEXT,
                url TEXT, tickers_json TEXT, raw_json TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO finnhub_news VALUES (?,?,?,?,?,?,?,?)",
            ("ACME", 77, "2026-01-06T16:00:00Z", "Acme gets a new contract",
             "https://wire.example/acme-contract", "Wire", None, "{}"),
        )
    with sqlite3.connect(runtime) as conn:
        conn.execute("CREATE TABLE run_artifacts (run_id TEXT, artifact_type TEXT, payload_json TEXT, event_seq INTEGER)")
        conn.execute(
            "INSERT INTO run_artifacts VALUES (?,?,?,?)",
            ("run-1", "context_pack", json.dumps({"included_evidence_ids": ["web:api:web_body:0001"]}), 1),
        )
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(bodies))
    monkeypatch.setenv("CATALYST_HEADLINE_ARCHIVE_DB", str(archive))
    monkeypatch.setenv("CATALYST_MEDIA_DB", str(tmp_path / "media.sqlite3"))
    fills = []
    monkeypatch.setattr(media, "headlines", lambda ticker, **kwargs: fills.append((ticker, kwargs)))
    app = FastAPI()
    app.include_router(router)
    app.state.db_path = runtime

    with TestClient(app) as client:
        response = client.get("/api/sessions/ACME/2026-01-06/news", params={"run_id": "run-1"})
        unknown = client.get("/api/sessions/MISSING/2026-01-06/news")
        non_session = client.get("/api/sessions/ACME/2026-01-10/news")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ticker"] == "ACME"
    assert payload["session_date"] == "2026-01-06"
    assert payload["items"][0]["used_as_evidence"] is True
    assert payload["items"][0]["published_at"] == "2026-01-06T15:00:00Z"
    assert payload["images_pending"] == 1
    assert len(fills) == 1 and fills[0][0] == "ACME"
    assert fills[0][1]["date"] == "2026-01-06"
    assert unknown.status_code == 404
    assert non_session.status_code == 422
