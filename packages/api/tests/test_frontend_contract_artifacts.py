from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from catalyst_app.events import PublicRunEvent
from catalyst_app.main import create_app
from fastapi.routing import APIRoute
from pydantic import TypeAdapter

REPO = Path(__file__).resolve().parents[3]
CONTRACTS = REPO / "contracts"
FIXTURES = CONTRACTS / "fixtures"


def _app_routes() -> dict[tuple[str, str], APIRoute]:
    app = create_app()
    pending = list(app.routes)
    seen: set[int] = set()
    api_routes: list[APIRoute] = []
    while pending:
        route = pending.pop()
        if id(route) in seen:
            continue
        seen.add(id(route))
        if isinstance(route, APIRoute):
            api_routes.append(route)
            continue
        # FastAPI 0.140 represents included routers lazily in app.routes.
        # Earlier versions expose their APIRoutes directly.
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            pending.extend(original_router.routes)
    return {
        (method, route.path): route
        for route in api_routes
        for method in route.methods or ()
    }


def test_committed_openapi_matches_app() -> None:
    spec = importlib.util.spec_from_file_location(
        "export_api_contract", REPO / "scripts/export_api_contract.py"
    )
    assert spec is not None and spec.loader is not None
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)

    expected = exporter.render_openapi(create_app().openapi())
    actual = (CONTRACTS / "openapi.json").read_text(encoding="utf-8")
    assert actual == expected, (
        "contracts/openapi.json is stale; regenerate it with "
        "python scripts/export_api_contract.py"
    )


def test_openapi_exposes_event_time_grade_projection() -> None:
    answer = create_app().openapi()["components"]["schemas"]["RunViewAnswerDTO"]
    properties = answer["properties"]
    assert {"grade", "grade_label", "key_sentence", "event_sentence", "sources"} <= properties.keys()
    assert properties["grade"]["anyOf"][0]["enum"] == [
        "ORDINARY",
        "MOVED_WITH_GROUP",
        "CONFIRMED",
        "REPORTED",
        "NO_CLEAR_CAUSE",
        "SUPPORTED",
    ]


def test_ui_endpoints_have_explicit_response_models_and_sse_docs() -> None:
    routes = _app_routes()
    expected = {
        ("GET", "/api/sessions/{ticker}/{session_date}/news"),
        ("POST", "/api/sessions/{ticker}/{session_date}/web-leads"),
        ("POST", "/api/live-runs"),
        ("GET", "/api/live-runs/{run_id}/view"),
        ("GET", "/api/live-runs/{run_id}/events"),
        ("GET", "/api/live-runs/{run_id}/stream"),
        ("POST", "/api/live-runs/{run_id}/verify-source"),
        ("POST", "/api/live-runs/{run_id}/cancel"),
        ("GET", "/api/data/jobs/latest"),
        ("POST", "/api/data/update"),
        ("GET", "/api/quality"),
        ("GET", "/api/models/catalog"),
        ("POST", "/api/models/validate"),
    }
    assert expected <= routes.keys()
    for key in expected - {
        ("GET", "/api/live-runs/{run_id}/stream"),
    }:
        assert routes[key].response_model is not None, f"missing response model: {key}"

    spec = create_app().openapi()
    stream = spec["paths"]["/api/live-runs/{run_id}/stream"]["get"]
    assert "text/event-stream" in stream["responses"]["200"]["content"]


def test_event_payload_schemas_cover_every_public_event() -> None:
    from catalyst_app.events import EVENT_PAYLOAD_TYPES

    schemas = json.loads((CONTRACTS / "events.schema.json").read_text(encoding="utf-8"))
    assert set(schemas) == {event.value for event in EVENT_PAYLOAD_TYPES}
    for event_type, payload_type in EVENT_PAYLOAD_TYPES.items():
        assert schemas[event_type.value] == payload_type.model_json_schema()


def test_each_fixture_response_validates_against_its_endpoint_model() -> None:
    routes = _app_routes()
    fixture_files = sorted(FIXTURES.glob("*.json"))
    assert fixture_files, "contracts/fixtures must contain page-state fixtures"
    for path in fixture_files:
        fixture = json.loads(path.read_text(encoding="utf-8"))
        request = fixture["request"]
        if request.get("kind") == "sse-events":
            for line in path.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines():
                PublicRunEvent.model_validate_json(line)
            continue
        method = request.get("method")
        route_path = request.get("route")
        route = routes[(method, route_path)]
        assert route.response_model is not None, f"{path.name} route has no response model"
        TypeAdapter(route.response_model).validate_python(fixture["response"])
