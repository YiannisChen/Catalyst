from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.routers import public_access
from catalyst_app.runtime.budget import BudgetLedger
from catalyst_data.ingestion.tavily_ledger import TavilyCallContext, guard
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_tavily_limit_and_public_switch_defaults(monkeypatch):
    monkeypatch.delenv("CATALYST_DAILY_TAVILY_CALL_LIMIT", raising=False)
    monkeypatch.delenv("CATALYST_PUBLIC_TAVILY_ENABLED", raising=False)

    ledger = BudgetLedger.from_environment()

    assert ledger.daily_tavily_call_limit == 50
    assert ledger.public_tavily_enabled is False


def test_quota_reports_real_tavily_calls_for_current_utc_day(tmp_path, monkeypatch):
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(public_access, "utc_now", lambda: now)
    monkeypatch.setenv("CATALYST_DAILY_TAVILY_CALL_LIMIT", "2")

    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as conn:
        init_runtime_db(conn)

    context = TavilyCallContext(
        db_path=db_path, tier="admin", daily_limit=2, public_enabled=False,
    )
    with guard(context, now=now):
        pass
    with guard(context, now=now):
        pass
    with guard(context, now=datetime(2026, 11, 4, 12, tzinfo=timezone.utc)):
        pass
    try:
        with guard(context, now=now):
            raise RuntimeError("failed call")
    except RuntimeError:
        pass

    app = FastAPI()
    app.include_router(public_access.router)
    app.state.db_path = db_path
    with TestClient(app) as client:
        response = client.get("/api/quota")

    assert response.status_code == 200
    tavily = response.json()["global"]["tavily_credits"]
    assert tavily == {"limit": 2, "used": 2}
