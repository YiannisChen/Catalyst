from catalyst_app import dependencies
from catalyst_app.routers import live_runs, sessions
from catalyst_app.runtime.admission import build_verify_source_admission_request
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app():
    app = FastAPI()
    app.include_router(sessions.router)
    return TestClient(app)


def test_web_leads_route_passes_header_and_projects_response(monkeypatch):
    seen = {}

    def search(ticker, session_date, **kwargs):
        seen.update(ticker=ticker, session_date=session_date, **kwargs)
        return {
            "ticker": ticker,
            "session_date": session_date,
            "leads": [{"lead_id": "id", "title": "A", "url": "https://example.com/a",
                       "publisher": "Example", "published_at": "2026-04-23T18:00:00Z",
                       "time_source": "jsonld.datePublished", "snippet": "s", "image_url": None,
                       "mentions_subject": True, "mentions_in_title": True,
                       "also_published_by": [], "status": "PRE"}],
            "excluded": {"post_close": 0, "undated": 0, "unfetched": 0, "off_subject": 0},
            "queries_used": ["a", "b", "c"], "credits_used": 3, "cached": False,
            "provider": "tavily", "label": "web leads — not verified causes",
        }

    monkeypatch.setattr(sessions.web_search, "search_leads", search)
    with _app() as client:
        response = client.post(
            "/api/sessions/tsla/2026-04-23/web-leads",
            json={"hints": ["contract"]},
            headers={"X-Tavily-Key": "never-return-this"},
        )
    assert response.status_code == 200
    assert seen["ticker"] == "TSLA" and seen["api_key"] == "never-return-this"
    assert response.json()["leads"][0]["status"] == "PRE"
    assert "never-return-this" not in response.text


def test_web_leads_route_maps_missing_key_and_hint_bounds(monkeypatch):
    def missing(*args, **kwargs):
        raise sessions.web_search.WebSearchError("tavily_key_missing")

    monkeypatch.setattr(sessions.web_search, "search_leads", missing)
    with _app() as client:
        response = client.post("/api/sessions/NOW/2026-07-30/web-leads", json={})
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "tavily_key_missing"
        too_many = client.post("/api/sessions/NOW/2026-07-30/web-leads", json={"hints": ["a", "b", "c"]})
    assert too_many.status_code == 422


def test_web_leads_route_passes_anonymous_public_switch_context(tmp_path, monkeypatch):
    from catalyst_app.access_control import install_access_control

    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.delenv("CATALYST_PUBLIC_TAVILY_ENABLED", raising=False)
    seen = {}

    def search(ticker, session_date, **kwargs):
        seen.update(kwargs)
        return {
            "ticker": ticker,
            "session_date": session_date,
            "leads": [],
            "excluded": {"post_close": 0, "undated": 0, "unfetched": 0, "off_subject": 0},
            "queries_used": [], "credits_used": 0, "cached": False,
            "provider": "tavily", "label": "web leads — not verified causes",
        }

    monkeypatch.setattr(sessions.web_search, "search_leads", search)
    app = FastAPI()
    app.include_router(sessions.router)
    app.state.db_path = tmp_path / "runtime.sqlite3"
    install_access_control(app)

    with TestClient(app) as client:
        response = client.post("/api/sessions/NOW/2026-07-30/web-leads", json={})

    assert response.status_code == 200
    context = seen["tavily_context"]
    assert context.tier == "anonymous"
    assert context.public_enabled is False


def test_verify_source_creates_child_run_bound_to_parent_and_pre_lead(monkeypatch):
    import json
    from contextlib import contextmanager
    from types import SimpleNamespace

    manifest = {"temporal_identity": {"session_date": "2026-04-23"}, "analyst_model_id": "deepseek-chat"}

    class Rows:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class FakeConnection:
        def execute(self, sql, params):
            if "FROM runs" in sql:
                return Rows({"ticker": "TSLA", "provider": "deepseek", "base_url": "https://api.deepseek.com/v1"})
            return Rows({"payload_json": json.dumps(manifest)})

    @contextmanager
    def fake_open(_path):
        yield FakeConnection()

    lead = {"ticker": "TSLA", "session_date": "2026-04-23", "lead_id": "a" * 64}
    monkeypatch.setattr(live_runs, "open_readonly", fake_open)
    monkeypatch.setattr("catalyst_data.ingestion.collectors.web_search.get_pre_lead", lambda _id: lead if _id == "a" * 64 else None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "model-secret-value")

    class CredentialStore:
        def register(self, run_id, *, api_key):
            self.registered = (run_id, api_key)

    credentials = CredentialStore()

    class Admission:
        def admit(self, request, *, pre_submit):
            self.request = request
            pre_submit("child-run")
            return SimpleNamespace(kind="accepted", run_id="child-run", stream_url="/api/live-runs/child-run/stream", request_hash="1234567890")

    admission = Admission()
    app = FastAPI()
    app.include_router(live_runs.router)
    app.state.db_path = "/tmp/fake-live.sqlite3"
    app.state.admission_controller = admission
    app.dependency_overrides[dependencies.get_credential_store] = lambda: credentials
    with TestClient(app) as client:
        response = client.post("/api/live-runs/parent/verify-source", json={"lead_id": "a" * 64})
    assert response.status_code == 200
    assert admission.request.parent_run_id == "parent"
    assert admission.request.web_search_lead_id == "a" * 64
    assert admission.request.force_full is True
    assert admission.request.origin == "http"
    assert admission.request.budget.tier == "admin"
    assert credentials.registered == ("child-run", "model-secret-value")
    assert "model-secret-value" not in response.text


def test_verify_source_is_admin_only_in_public_mode_and_ignores_client_tier(monkeypatch):
    from catalyst_app.access_control import install_access_control

    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    app = FastAPI()
    app.include_router(live_runs.router)
    install_access_control(app)

    with TestClient(app) as client:
        response = client.post(
            "/api/live-runs/parent/verify-source",
            json={"lead_id": "a" * 64, "tier": "local", "access_tier": "admin"},
            headers={"X-Access-Tier": "local", "X-Admin": "true"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "admin_unauthenticated"}


def test_verify_source_admission_request_matches_child_run_policy():
    request = build_verify_source_admission_request(
        ticker="TSLA",
        session_date="2026-04-23",
        provider="deepseek",
        model_id="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
        lead_id="a" * 64,
        parent_run_id=None,
    )

    assert request.ticker == "TSLA"
    assert request.session_date == "2026-04-23"
    assert request.query == f"verify-source:{'a' * 64}"
    assert request.provider == "deepseek"
    assert request.model_id == "deepseek-chat"
    assert request.base_url == "https://api.deepseek.com/v1"
    assert request.credential_source_identifier == "server_env"
    assert request.workflow_version == "v1.1-web-source"
    assert request.config_version == "v1.1"
    assert request.force_full is True
    assert request.parent_run_id is None
    assert request.web_search_lead_id == "a" * 64
    assert request.origin == "http"


def test_verify_source_rejects_missing_and_mismatched_leads(monkeypatch):
    import json
    from contextlib import contextmanager

    manifest = {"temporal_identity": {"session_date": "2026-04-23"}, "analyst_model_id": "deepseek-chat"}

    class Rows:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class FakeConnection:
        def execute(self, sql, params):
            if "FROM runs" in sql:
                return Rows({"ticker": "TSLA", "provider": "deepseek", "base_url": None})
            return Rows({"payload_json": json.dumps(manifest)})

    @contextmanager
    def fake_open(_path):
        yield FakeConnection()

    monkeypatch.setattr(live_runs, "open_readonly", fake_open)
    lead = {"ticker": "NOW", "session_date": "2026-04-23", "lead_id": "b" * 64}
    monkeypatch.setattr("catalyst_data.ingestion.collectors.web_search.get_pre_lead", lambda _id: lead if _id == "b" * 64 else None)
    app = FastAPI()
    app.include_router(live_runs.router)
    app.state.db_path = "/tmp/fake-live.sqlite3"
    with TestClient(app) as client:
        missing = client.post("/api/live-runs/parent/verify-source", json={"lead_id": "c" * 64})
        mismatch = client.post("/api/live-runs/parent/verify-source", json={"lead_id": "b" * 64})
    assert missing.status_code == 404
    assert mismatch.status_code == 409


def test_web_lead_retriever_serves_only_the_selected_page_as_full_text():
    from catalyst_app.runtime.composition import _WebLeadRetriever
    from catalyst_data.canonical.temporal import build_temporal_identity

    lead = {
        "lead_id": "d" * 64,
        "url": "https://publisher.example/story",
        "title": "Tesla announces a factory",
        "publisher": "Publisher",
        "published_at": "2026-04-23T18:00:00Z",
        "extracted_text": "Tesla announced a factory. " * 30,
    }
    identity = build_temporal_identity("2026-04-23")
    evidence = _WebLeadRetriever(lead).retrieve(
        "ignored", ticker="TSLA", cutoff=identity.cutoff_at.isoformat(),
        requested_manifest_id="manifest", temporal_identity=identity,
        top_k=20, candidate_depth=100, source_classes=(),
    )
    assert len(evidence) == 1
    item = evidence[0].to_evidence_state_item(first_seen_round=1, contributing_task_ids=("web",))
    assert item.content_state == "FULL_TEXT"
    assert item.canonical_url == lead["url"]
    assert item.excerpt_text == lead["extracted_text"]
