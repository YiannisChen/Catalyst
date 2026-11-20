from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from catalyst_app.access_control import sign_invite_token
from catalyst_app.deployment_configuration import PUBLIC_DEPLOYMENT_PINNED_ENV
from catalyst_app.events import (
    AnalystDecisionPayload,
    AnalystDonePayload,
    AnalystEvidenceDecision,
    AnalystHypothesis,
    AnswerCompletedPayload,
    AnswerDeltaPayload,
    AnswerReadyPayload,
    AnswerStartedPayload,
    AssuranceCompletedPayload,
    CheckedQuote,
    CheckGate,
    ChecksDonePayload,
    EvidenceAssessedPayload,
    EvidenceRetrievedPayload,
    FactsReadyPayload,
    FactsReadyWindow,
    FollowUpStartedPayload,
    GatesDonePayload,
    InvestigatorDonePayload,
    PublicRunEvent,
    RetrievalDoneItem,
    RetrievalDonePayload,
    RunAcceptedPayload,
    RunCancelledPayload,
    RunCompletedPayload,
    RunEventType,
    RunFailedPayload,
    StageStartedPayload,
    TierDecidedPayload,
    VerifierDonePayload,
)
from catalyst_app.main import create_app
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.public_projection import (
    _PUBLIC_EVENT_PAYLOAD_FIELDS,
    is_benzinga_source,
    must_hide_source_text,
    project_public_answer,
    project_public_event,
    public_event_long_string_violations,
)
from catalyst_app.routers import public_access, sessions
from fastapi import FastAPI
from fastapi.testclient import TestClient

_BENZINGA_URL = "https://www.benzinga.com/news/acme-move"
_BENZINGA_OTHER_URL = "https://www.benzinga.com/news/acme-other"
_Q1 = (
    "Benzinga reported the first complete cause sentence. "
    "The second complete sentence explains the reaction. "
    "A third sentence must stay private."
)
_Q2 = "This duplicate Benzinga sentence must not consume another budget. Another sentence is also private."
_Q3 = (
    "Another Benzinga article starts with its first public sentence. "
    "Its second public sentence completes the allowed pair. "
    "Its third sentence is private."
)
_Q_NO_URL = "Benzinga text without a URL must stay private. It has two complete sentences."
_NON_BENZINGA = (
    "The wire published one sentence. "
    "The wire published a second sentence. "
    "The wire published a third sentence."
)


def _snapshot() -> dict[str, object]:
    return {
        "schema_version": "answer_snapshot_v1",
        "run_id": "run:public",
        "ticker": "ACME",
        "session_date": "2026-01-06",
        "headline": "ACME rose 2%.",
        "outcome": "ANSWERED",
        "status": "PARTIAL",
        "tier": "T3",
        "key_sentence": {"text": _Q1, "publisher": "Benzinga", "url": _BENZINGA_URL},
        "also_sentence": {"text": _Q2, "publisher": "Benzinga", "url": _BENZINGA_URL},
        "event_sentence": {"text": _Q2, "publisher": "Benzinga", "url": _BENZINGA_URL},
        "sources": [
            {
                "text": _Q1,
                "publisher": "Benzinga",
                "url": _BENZINGA_URL,
                "evidence_id": "ev-q1",
                "role": "PRIMARY",
                "claim_id": "private-claim-id",
            },
            {"text": _Q2, "publisher": "Benzinga", "url": _BENZINGA_URL, "evidence_id": "ev-q2"},
            {"text": _Q_NO_URL, "publisher": "Benzinga", "evidence_id": "ev-no-url"},
            {"text": _Q3, "publisher": "Benzinga", "url": _BENZINGA_OTHER_URL, "role": "CONTEXT"},
            {
                "text": _NON_BENZINGA,
                "publisher": "Example Wire",
                "url": "https://wire.example/acme",
                "role": "CONTEXT",
            },
        ],
        "primary": {"quotes": [{"text": _Q1, "publisher": "Benzinga", "url": _BENZINGA_URL}]},
        "abstention": {
            "possible_explanation": {
                "quotes": [{"text": _Q2, "publisher": "Benzinga", "url": _BENZINGA_URL}],
                "verdict_category": "UNLINKED",
            }
        },
        "investigation": {
            "mode": "agent_v3",
            "stop_reason": "model_stop",
            "steps": [
                {
                    "step": 1,
                    "tool": "search_news",
                    "status": "OK",
                    "summary": "SECRET_INVESTIGATION_SUMMARY",
                    "search_hint": "SECRET_SEARCH_HINT",
                    "n_facts": 2,
                    "board_after": [{"kind": "COMPANY", "status": "SUPPORTED", "note": "SECRET_BOARD_NOTE"}],
                }
            ],
            "board": [{"kind": "COMPANY", "status": "SUPPORTED", "note": "SECRET_BOARD_NOTE"}],
        },
        "excerpt_text": "SECRET_UNDECLARED_ANSWER_FIELD",
    }


def _seed_run(
    db_path: Path,
    run_id: str,
    *,
    snapshot: dict[str, object] | None = None,
    trace: dict[str, object] | None = None,
    legacy_text: str | None = None,
    visibility: str = "public",
) -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, request_hash, run_manifest_id,"
            " manifest_hash, capacity_slot, created_at, updated_at, ticker, provider,"
            " visibility, access_tier) VALUES (?, 'COMPLETED', ?, ?, ?, 0, ?, ?, 'ACME',"
            " 'openai', ?, 'anonymous')",
            (
                run_id,
                hashlib.sha256(run_id.encode()).hexdigest(),
                f"manifest:{run_id}",
                "a" * 64,
                "2026-01-06T21:00:00Z",
                "2026-01-06T21:00:03Z",
                visibility,
            ),
        )
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage,"
            " payload_json, schema_version) VALUES (?, 1, '2026-01-06T21:00:00Z',"
            " 'run.accepted', 'ADMISSION', ?, 'v1')",
            (
                run_id,
                json.dumps(
                    {
                        "request_identity_digest": "d" * 64,
                        "ticker": "ACME",
                        "trade_date": "2026-01-06",
                        "workflow_version": "v1.1",
                        "model_provider_label": "openai",
                        "stream_url": f"/api/live-runs/{run_id}/stream",
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage,"
            " payload_json, schema_version) VALUES (?, 2, '2026-01-06T21:00:03Z',"
            " 'run.completed', 'TERMINAL', ?, 'v1')",
            (
                run_id,
                json.dumps(
                    {
                        "result_status": "SUCCEEDED",
                        "final_output_artifact_ref": f"artifact:answer:{run_id}",
                        "total_latency_ms": 3000,
                        "runtime_identity_ref": "test",
                    }
                ),
            ),
        )
        artifacts: dict[str, object] = {
            "run_manifest": {
                "temporal_identity": {"session_date": "2026-01-06"},
                "analyst_model_id": "test-model",
                "attribution_policy": {"verifier_enabled": True},
            }
        }
        if snapshot is not None:
            artifacts["answer_snapshot"] = snapshot
        if trace is not None:
            artifacts["decision_trace"] = trace
        if legacy_text is not None:
            artifacts["answer"] = {"text": legacy_text}
        for index, (artifact_type, payload) in enumerate(artifacts.items()):
            conn.execute(
                "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type,"
                " payload_hash, payload_json, optional) VALUES (?, ?, 1, ?, ?, ?, 0)",
                (f"{artifact_type}:{run_id}:{index}", run_id, artifact_type, "b" * 64, json.dumps(payload)),
            )
        conn.commit()


def _link_session(db_path: Path, run_id: str, *, day: str, token: str) -> None:
    with open_rw(db_path) as conn:
        conn.execute(
            "INSERT INTO anonymous_session_runs (session_hash, utc_day, run_id, created_at)"
            " VALUES (?, ?, ?, ?)",
            (hashlib.sha256(token.encode()).hexdigest(), day, run_id, f"{day}T12:00:00Z"),
        )
        conn.commit()


def _public_client(db_path: Path, monkeypatch, *, admin_token: str = "test-admin-token"):
    for key, value in PUBLIC_DEPLOYMENT_PINNED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", admin_token)
    monkeypatch.setenv("CATALYST_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("CATALYST_ALLOWED_ORIGINS", "http://testserver")
    monkeypatch.setattr(public_access, "utc_now", lambda: datetime(2026, 1, 6, 18, tzinfo=timezone.utc))
    return create_app(db_path=db_path)


def _session_cookie() -> tuple[str, str, str]:
    token = "s" * 40
    day = "2026-01-06"
    return public_access.ANONYMOUS_SESSION_COOKIE, f"{day}.{token}", token


def _answer_source_texts(answer: dict[str, object]) -> list[str]:
    texts: list[str] = []
    for field in ("key_sentence", "also_sentence", "event_sentence"):
        source = answer.get(field)
        if isinstance(source, dict) and isinstance(source.get("text"), str):
            texts.append(source["text"])
    for source in answer.get("sources") or ():
        if isinstance(source, dict) and isinstance(source.get("text"), str):
            texts.append(source["text"])
    for field in ("primary", "secondary"):
        block = answer.get(field)
        if isinstance(block, dict):
            for source in block.get("quotes") or ():
                if isinstance(source, dict) and isinstance(source.get("text"), str):
                    texts.append(source["text"])
    abstention = answer.get("abstention")
    if isinstance(abstention, dict):
        possible = abstention.get("possible_explanation")
        if isinstance(possible, dict):
            for source in possible.get("quotes") or ():
                if isinstance(source, dict) and isinstance(source.get("text"), str):
                    texts.append(source["text"])
    return texts



@pytest.fixture(autouse=True)
def _source_limits_on(monkeypatch):
    """These tests cover the Benzinga/CNBC visitor limits, which are opt-in since 2026-11-18."""
    monkeypatch.setenv("CATALYST_PUBLIC_SOURCE_LIMITS", "1")

def test_non_admin_view_projects_benzinga_and_private_text(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "runtime.db"
    trace = {
        "schema_version": "decision_trace_v1",
        "free_text": "SECRET_TRACE_TEXT",
        "desk_decision": {
            "clusters": [{
                "best_attribution": {"quote_sha256": "a" * 64, "evidence_id": "ev-private", "roles": ["LEAD"]},
                "best_event": {"quote_sha256": "b" * 64, "evidence_id": "ev-private", "roles": ["EVENT"]},
                "pairing_rule": "rejected_lead_same_source_event",
            }]
        },
    }
    _seed_run(db_path, "run:public", snapshot=_snapshot(), trace=trace)
    cookie_name, cookie_value, token = _session_cookie()
    _link_session(db_path, "run:public", day="2026-01-06", token=token)

    with TestClient(_public_client(db_path, monkeypatch)) as client:
        client.cookies.set(cookie_name, cookie_value)
        response = client.get("/api/live-runs/run:public/view")

    assert response.status_code == 200
    body = response.json()
    answer = body["answer"]
    assert body["trace"] is None
    assert "best_attribution" not in response.text
    assert "best_event" not in response.text
    assert "rejected_lead_same_source_event" not in response.text
    assert "ev-private" not in response.text
    assert body.get("legacy_text") is None
    assert "SECRET_TRACE_TEXT" not in response.text
    assert "SECRET_INVESTIGATION_SUMMARY" not in response.text
    assert "SECRET_SEARCH_HINT" not in response.text
    assert "SECRET_BOARD_NOTE" not in response.text
    assert "SECRET_UNDECLARED_ANSWER_FIELD" not in response.text
    assert "private-claim-id" not in response.text
    assert _Q2 not in response.text
    assert _Q_NO_URL not in response.text

    assert answer["key_sentence"]["text"] == (
        "Benzinga reported the first complete cause sentence. "
        "The second complete sentence explains the reaction."
    )
    assert "text" not in answer["also_sentence"]
    assert answer["also_sentence"]["publisher"] == "Benzinga"
    assert answer["also_sentence"]["url"] == _BENZINGA_URL
    assert answer["sources"][1]["evidence_id"] == "ev-q2"
    assert "text" not in answer["sources"][1]
    assert answer["sources"][2]["text"].startswith("Another Benzinga article starts")
    assert answer["sources"][2]["text"].endswith("completes the allowed pair.")
    assert _NON_BENZINGA in _answer_source_texts(answer)
    assert answer["investigation"]["mode"] == "agent_v3"
    assert answer["investigation"]["stop_reason"] == "model_stop"
    assert answer["investigation"]["steps"][0]["n_facts"] == 2
    assert set(answer["investigation"]["steps"][0]) <= {
        "step", "mode", "action", "tool", "hypothesis", "forced", "status",
        "n_facts", "n_rejected", "new_evidence", "board_after",
    }
    assert set(answer["investigation"]["board"][0]) == {"kind", "status"}
    for source in answer["sources"]:
        assert set(source) <= {
            "text", "publisher", "url", "evidence_id", "role",
            "published_at", "published_precision",
        }

    for text in _answer_source_texts(answer):
        if text == _NON_BENZINGA:
            continue
        assert text.count(".") <= 2
        assert any(text in stored for stored in (_Q1, _Q2, _Q3, _Q_NO_URL, _NON_BENZINGA))


def test_admin_view_keeps_full_text_and_trace(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "runtime.db"
    trace = {"schema_version": "decision_trace_v1", "free_text": "SECRET_TRACE_TEXT"}
    _seed_run(db_path, "run:public", snapshot=_snapshot(), trace=trace, visibility="private")

    with TestClient(_public_client(db_path, monkeypatch)) as client:
        response = client.get(
            "/api/live-runs/run:public/view",
            headers={"Authorization": "Bearer test-admin-token"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["trace"]["free_text"] == "SECRET_TRACE_TEXT"
    assert body["answer"]["also_sentence"]["text"] == _Q2
    assert body["answer"]["investigation"]["steps"][0]["summary"] == "SECRET_INVESTIGATION_SUMMARY"
    assert body["answer"]["investigation"]["board"][0]["note"] == "SECRET_BOARD_NOTE"
    assert any(source["text"] == _Q2 for source in body["answer"]["sources"])


def test_view_denies_another_anonymous_session(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, "run:mine", snapshot={"outcome": "ABSTAINED"})
    _seed_run(db_path, "run:other", snapshot={"outcome": "ABSTAINED"})
    cookie_name, cookie_value, token = _session_cookie()
    _link_session(db_path, "run:mine", day="2026-01-06", token=token)

    with TestClient(_public_client(db_path, monkeypatch)) as client:
        client.cookies.set(cookie_name, cookie_value)
        mine = client.get("/api/live-runs/run:mine/view")
        other = client.get("/api/live-runs/run:other/view")
        admin = client.get(
            "/api/live-runs/run:other/view",
            headers={"Authorization": "Bearer test-admin-token"},
        )
        client.cookies.clear()
        no_session = client.get("/api/live-runs/run:mine/view")

    assert mine.status_code == 200
    assert other.status_code == 404
    assert other.json() == {"detail": "run_not_found"}
    assert admin.status_code == 200
    assert no_session.status_code == 404


def test_view_invite_reads_own_and_public_runs_only(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, "run:invite", snapshot={"outcome": "ABSTAINED"}, visibility="private")
    _seed_run(db_path, "run:public", snapshot={"outcome": "ABSTAINED"}, visibility="public")
    _seed_run(db_path, "run:other", snapshot={"outcome": "ABSTAINED"}, visibility="private")
    with open_rw(db_path) as conn:
        conn.execute(
            "INSERT INTO run_budget_reservations (run_id, utc_day, tier, caller_key,"
            " quota_charged, global_budget_charged, max_run_cost_micros,"
            " reserved_cost_micros, settled_cost_micros, created_at)"
            " VALUES ('run:invite', '2026-01-06', 'invite', 'invite-1', 1, 0, 50000, 50000, 0,"
            " '2026-01-06T12:00:00Z')"
        )
        conn.commit()
    monkeypatch.setenv("CATALYST_INVITE_SIGNING_KEY", "test-invite-signing-key-which-is-at-least-32-bytes")
    invite = sign_invite_token(
        "invite-1", (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    )

    with TestClient(_public_client(db_path, monkeypatch)) as client:
        headers = {"X-Invite-Token": invite}
        own = client.get("/api/live-runs/run:invite/view", headers=headers)
        public = client.get("/api/live-runs/run:public/view", headers=headers)
        other = client.get("/api/live-runs/run:other/view", headers=headers)

    assert own.status_code == 200
    assert public.status_code == 200
    assert other.status_code == 404


def test_non_admin_view_omits_legacy_text(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, "run:legacy", legacy_text="LEGACY BENZINGA BODY TEXT")
    cookie_name, cookie_value, token = _session_cookie()
    _link_session(db_path, "run:legacy", day="2026-01-06", token=token)

    with TestClient(_public_client(db_path, monkeypatch)) as client:
        client.cookies.set(cookie_name, cookie_value)
        response = client.get("/api/live-runs/run:legacy/view")

    assert response.status_code == 200
    assert response.json().get("legacy_text") is None
    assert "LEGACY BENZINGA BODY TEXT" not in response.text


def _seed_stream_run(
    db_path: Path,
    snapshot: dict[str, object],
    *,
    investigator_steps: list[dict[str, object]] | None = None,
) -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, request_hash, run_manifest_id,"
            " manifest_hash, capacity_slot, created_at, updated_at, ticker, provider,"
            " visibility, access_tier) VALUES ('run:stream', 'COMPLETED', ?, 'manifest:stream',"
            " ?, 0, '2026-01-06T21:00:00Z', '2026-01-06T21:00:03Z', 'ACME', 'openai',"
            " 'public', 'anonymous')",
            ("e" * 64, "f" * 64),
        )
        events = [
            (
                "evidence.assessed",
                {
                    "accepted_evidence_ids": [],
                    "lead_only_evidence_ids": [],
                    "rejected_evidence_ids": [],
                    "reason_codes": [],
                    "gap_ids": [],
                    "status_ceiling": "ABSTAIN",
                    "decision": "ABSTAIN",
                    "normalization_diagnostics": [
                        "QUOTE_OK:h1:hash:web_body:0001:Secret quote sentence one. Secret quote sentence two. Secret quote sentence three.",
                        "primary_verifier_supported:reason:VERIFIER_MODEL_REASONING_SECRET",
                    ],
                },
            ),
            ("answer.ready", {"answer_snapshot": snapshot}),
            (
                "investigator.done",
                {
                    "mode": "agent_v3",
                    "stop_reason": "model_stop",
                    "steps": investigator_steps or [
                        {
                            "tool": "search_news",
                            "status": "OK",
                            "summary": "SECRET_SSE_SUMMARY",
                            "search_hint": "SECRET_SSE_HINT",
                            "n_facts": 2,
                        }
                    ],
                    "evidence_added": 1,
                },
            ),
            (
                "answer.delta",
                {
                    "writer_stream_id": "w1",
                    "delta_text": _Q2,
                    "delta_ordinal": 1,
                    "cumulative_character_count": len(_Q2),
                },
            ),
            (
                "run.completed",
                {
                    "result_status": "SUCCEEDED",
                    "final_output_artifact_ref": "artifact:answer:run:stream",
                    "total_latency_ms": 3000,
                    "runtime_identity_ref": "test",
                },
            ),
        ]
        for seq, (event_type, payload) in enumerate(events, start=1):
            conn.execute(
                "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage,"
                " payload_json, schema_version) VALUES (?, ?, ?, ?, 'TEST', ?, 'v1')",
                ("run:stream", seq, f"2026-01-06T21:00:0{seq}Z", event_type, json.dumps(payload)),
            )
        conn.commit()


def test_non_admin_sse_projects_ready_and_investigator_and_suppresses_delta(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_stream_run(db_path, _snapshot())
    cookie_name, cookie_value, token = _session_cookie()
    _link_session(db_path, "run:stream", day="2026-01-06", token=token)

    with TestClient(_public_client(db_path, monkeypatch)) as client:
        client.cookies.set(cookie_name, cookie_value)
        response = client.get("/api/live-runs/run:stream/stream")

    assert response.status_code == 200
    assert "event: answer.ready" in response.text
    assert "event: investigator.done" in response.text
    assert "event: answer.delta" not in response.text
    assert "SECRET_SSE_SUMMARY" not in response.text
    assert "SECRET_SSE_HINT" not in response.text
    assert "SECRET_INVESTIGATION_SUMMARY" not in response.text
    assert "SECRET_BOARD_NOTE" not in response.text
    assert "normalization_diagnostics" not in response.text
    assert "QUOTE_OK" not in response.text
    assert "primary_verifier_supported" not in response.text
    assert "VERIFIER_MODEL_REASONING_SECRET" not in response.text
    assert _Q2 not in response.text

    with TestClient(_public_client(db_path, monkeypatch)) as client:
        admin = client.get(
            "/api/live-runs/run:stream/stream",
            headers={"Authorization": "Bearer test-admin-token"},
        )

    assert admin.status_code == 200
    assert "event: answer.delta" in admin.text
    assert "SECRET_SSE_SUMMARY" in admin.text
    assert "normalization_diagnostics" in admin.text
    assert "QUOTE_OK" in admin.text
    assert "VERIFIER_MODEL_REASONING_SECRET" in admin.text
    assert _Q2 in admin.text


def _news_databases(tmp_path: Path) -> tuple[Path, Path]:
    bodies = tmp_path / "bodies.sqlite3"
    archive = tmp_path / "archive.sqlite3"
    with sqlite3.connect(bodies) as conn:
        conn.executescript(
            """
            CREATE TABLE web_body_chunks (
                asset_id TEXT, ordinal INTEGER, chunk_id TEXT, text TEXT,
                canonical_url TEXT, title TEXT, publisher TEXT, eligible_at TEXT
            );
            CREATE TABLE web_headlines (asset_id TEXT, ticker TEXT, eligible_at TEXT);
            """
        )
        conn.executemany(
            "INSERT INTO web_body_chunks VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "asset:benz",
                    "chunk:benz",
                    "BENZINGA FULL BODY MUST STAY PRIVATE. Another body sentence.",
                    _BENZINGA_URL,
                    "ACME moves on Benzinga report",
                    "Benzinga",
                    "2026-01-06T15:00:00Z",
                ),
                (
                    "asset:wire",
                    "chunk:wire",
                    "The wire summary is public metadata.",
                    "https://wire.example/acme",
                    "ACME wire summary",
                    "Example Wire",
                    "2026-01-06T16:00:00Z",
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO web_headlines VALUES (?, 'ACME', ?)",
            [("asset:benz", "2026-01-06T15:00:00Z"), ("asset:wire", "2026-01-06T16:00:00Z")],
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
    return bodies, archive


def test_session_news_hides_benzinga_summary_for_non_admin(tmp_path: Path, monkeypatch) -> None:
    bodies, archive = _news_databases(tmp_path)
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(bodies))
    monkeypatch.setenv("CATALYST_HEADLINE_ARCHIVE_DB", str(archive))
    monkeypatch.setenv("CATALYST_MEDIA_DB", str(tmp_path / "media.sqlite3"))
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", "test-admin-token")
    app = FastAPI()
    app.include_router(sessions.router)

    with TestClient(app) as client:
        public = client.get("/api/sessions/ACME/2026-01-06/news")
        admin = client.get(
            "/api/sessions/ACME/2026-01-06/news",
            headers={"Authorization": "Bearer test-admin-token"},
        )

    assert public.status_code == 200
    public_items = {item["publisher"]: item for item in public.json()["items"]}
    assert public_items["Benzinga"]["summary"] is None
    assert public_items["Benzinga"]["title"] and public_items["Benzinga"]["url"]
    assert public_items["Example Wire"]["summary"] == "The wire summary is public metadata."
    admin_items = {item["publisher"]: item for item in admin.json()["items"]}
    assert admin_items["Benzinga"]["summary"].startswith("BENZINGA FULL BODY")


def test_web_leads_hides_benzinga_snippet_for_non_admin(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", "test-admin-token")

    def search(ticker, session_date, **kwargs):
        return {
            "ticker": ticker,
            "session_date": session_date,
            "leads": [
                {
                    "lead_id": "b" * 64,
                    "title": "Benzinga lead",
                    "url": _BENZINGA_URL,
                    "publisher": "Benzinga",
                    "published_at": "2026-01-06T18:00:00Z",
                    "time_source": "jsonld.datePublished",
                    "snippet": "BENZINGA SNIPPET MUST STAY PRIVATE",
                    "image_url": None,
                    "mentions_subject": True,
                    "mentions_in_title": True,
                    "also_published_by": [],
                    "status": "PRE",
                },
                {
                    "lead_id": "c" * 64,
                    "title": "Wire lead",
                    "url": "https://wire.example/acme",
                    "publisher": "Example Wire",
                    "published_at": "2026-01-06T18:00:00Z",
                    "time_source": "jsonld.datePublished",
                    "snippet": "Wire snippet",
                    "image_url": None,
                    "mentions_subject": True,
                    "mentions_in_title": True,
                    "also_published_by": [],
                    "status": "PRE",
                },
            ],
            "excluded": {"post_close": 0, "undated": 0, "unfetched": 0, "off_subject": 0},
            "queries_used": ["acme"],
            "credits_used": 1,
            "cached": False,
            "provider": "tavily",
            "label": "web leads — not verified causes",
        }

    monkeypatch.setattr(sessions.web_search, "search_leads", search)
    app = FastAPI()
    app.include_router(sessions.router)
    with TestClient(app) as client:
        public = client.post("/api/sessions/ACME/2026-01-06/web-leads", json={})
        admin = client.post(
            "/api/sessions/ACME/2026-01-06/web-leads",
            json={},
            headers={"Authorization": "Bearer test-admin-token"},
        )

    assert public.status_code == 200
    public_leads = {lead["publisher"]: lead for lead in public.json()["leads"]}
    assert public_leads["Benzinga"]["snippet"] is None
    assert public_leads["Example Wire"]["snippet"] == "Wire snippet"
    admin_leads = {lead["publisher"]: lead for lead in admin.json()["leads"]}
    assert admin_leads["Benzinga"]["snippet"] == "BENZINGA SNIPPET MUST STAY PRIVATE"


def test_review_defect_1_shared_budget_covers_hypothesis_and_sse_connection(
    tmp_path: Path, monkeypatch
) -> None:
    hypothesis_text = (
        "The hypothesis quote has one complete sentence. "
        "The hypothesis quote has a second complete sentence."
    )
    snapshot = _snapshot()
    snapshot["investigation"]["steps"][0]["hypothesis"] = {
        "text": hypothesis_text,
        "publisher": "Benzinga",
        "url": _BENZINGA_URL,
    }
    snapshot["investigation"]["steps"].append(
        {
            "step": 2,
            "tool": "search_news",
            "status": "OK",
            "n_facts": 1,
            "hypothesis": {
                "text": "A no-URL hypothesis must stay private. It has two sentences.",
                "publisher": "Benzinga",
            },
        }
    )
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, "run:hypothesis", snapshot=snapshot)
    cookie_name, cookie_value, token = _session_cookie()
    _link_session(db_path, "run:hypothesis", day="2026-01-06", token=token)

    with TestClient(_public_client(db_path, monkeypatch)) as client:
        client.cookies.set(cookie_name, cookie_value)
        view = client.get("/api/live-runs/run:hypothesis/view")

    assert view.status_code == 200
    hypothesis = view.json()["answer"]["investigation"]["steps"][0]["hypothesis"]
    assert hypothesis["url"] == _BENZINGA_URL
    assert "text" not in hypothesis
    no_url_step = view.json()["answer"]["investigation"]["steps"][1]
    assert no_url_step["tool"] == "search_news"
    assert no_url_step["n_facts"] == 1
    assert "hypothesis" not in no_url_step
    assert hypothesis_text not in view.text
    assert "A no-URL hypothesis must stay private" not in view.text

    stream_db = tmp_path / "stream.db"
    _seed_stream_run(
        stream_db,
        snapshot,
        investigator_steps=[
            {
                "tool": "search_news",
                "status": "OK",
                "hypothesis": {
                    "text": hypothesis_text,
                    "publisher": "Benzinga",
                    "url": _BENZINGA_URL,
                },
            }
        ],
    )
    stream_cookie_name, stream_cookie_value, stream_token = _session_cookie()
    _link_session(stream_db, "run:stream", day="2026-01-06", token=stream_token)
    with TestClient(_public_client(stream_db, monkeypatch)) as client:
        client.cookies.set(stream_cookie_name, stream_cookie_value)
        stream = client.get("/api/live-runs/run:stream/stream")

    assert stream.status_code == 200
    assert hypothesis_text not in stream.text


def test_review_defect_2_www_and_apex_urls_share_article_budget() -> None:
    first = "First shared sentence. Second shared sentence. Third shared sentence."
    second = "Another first sentence. Another second sentence. Another third sentence."
    answer = project_public_answer(
        {
            "outcome": "ANSWERED",
            "sources": [
                {
                    "text": first,
                    "publisher": "Benzinga",
                    "url": "https://www.benzinga.com/news/shared",
                    "evidence_id": "ev-1",
                },
                {
                    "text": second,
                    "publisher": "Benzinga",
                    "url": "https://benzinga.com/news/shared",
                    "evidence_id": "ev-2",
                },
            ],
        }
    )

    assert answer["sources"][0]["text"] == "First shared sentence. Second shared sentence."
    assert "text" not in answer["sources"][1]
    assert answer["sources"][1]["evidence_id"] == "ev-2"


def test_review_defect_3_benzinga_detection_fails_closed() -> None:
    assert must_hide_source_text("Partner Wire", " https://www.benzinga.com/news/acme")
    assert must_hide_source_text("Partner Wire", "www.benzinga.com/news/acme")
    assert must_hide_source_text("Partner Wire", "https://partner.example/benzinga/acme")
    assert must_hide_source_text("Partner Wire", "http://[")
    assert is_benzinga_source("Partner Wire", "http://[")
    assert not must_hide_source_text("Partner Wire", "https://partner.example/acme")

    projected = project_public_answer(
        {
            "outcome": "ANSWERED",
            "sources": [
                {
                    "text": "Unsafe first sentence. Unsafe second sentence.",
                    "publisher": "Partner Wire",
                    "url": "http://[",
                    "evidence_id": "ev-bad-url",
                }
            ],
        }
    )
    assert projected["sources"][0]["evidence_id"] == "ev-bad-url"
    assert "text" not in projected["sources"][0]


def test_review_defect_4_open_answer_fields_and_abstention_details_drop_text() -> None:
    projected = project_public_answer(
        {
            "outcome": "ABSTAINED",
            "abstention": {
                "reason_code": "NO_STOCK_NEWS",
                "details": {
                    "nested": {
                        "text": "SECRET_DETAIL_TEXT",
                        "quote": "SECRET_DETAIL_QUOTE",
                    }
                },
            },
            "facts": {
                "comove_sentence": "Public code-derived fact.",
                "nested": {"text": "SECRET_FACT_TEXT", "snippet": "SECRET_FACT_SNIPPET"},
            },
            "web_evidence": {"used_rows": [{"body": "SECRET_WEB_BODY", "url": "https://example.com"}]},
            "desk_context": {"best": {"kind": "MARKET"}, "note": "SECRET_DESK_NOTE"},
            "reported_by": [{"publisher": "Benzinga", "note": "SECRET_REPORTED_NOTE"}],
        }
    )
    rendered = json.dumps(projected)

    assert projected["abstention"]["details"] == {}
    assert projected["facts"]["comove_sentence"] == "Public code-derived fact."
    assert projected["desk_context"]["best"]["kind"] == "MARKET"
    assert projected["web_evidence"]["used_rows"][0]["url"] == "https://example.com"
    for secret in (
        "SECRET_DETAIL_TEXT",
        "SECRET_DETAIL_QUOTE",
        "SECRET_FACT_TEXT",
        "SECRET_FACT_SNIPPET",
        "SECRET_WEB_BODY",
        "SECRET_DESK_NOTE",
        "SECRET_REPORTED_NOTE",
    ):
        assert secret not in rendered


def test_review_defect_5_cited_source_keeps_metadata_after_budget_exhaustion() -> None:
    url = "https://www.benzinga.com/news/cited"
    projected = project_public_answer(
        {
            "outcome": "ANSWERED",
            "sources": [
                {
                    "text": "Cited first sentence. Cited second sentence. Cited third sentence.",
                    "publisher": "Benzinga",
                    "url": url,
                    "evidence_id": "ev-1",
                    "role": "PRIMARY",
                },
                {
                    "text": "Later first sentence. Later second sentence.",
                    "publisher": "Benzinga",
                    "url": url,
                    "evidence_id": "ev-2",
                    "role": "SECONDARY",
                },
                {
                    "text": "No URL must stay private. It has two sentences.",
                    "publisher": "Benzinga",
                    "evidence_id": "ev-no-url",
                },
            ],
        }
    )

    rows = {row["evidence_id"]: row for row in projected["sources"]}
    assert set(rows) == {"ev-1", "ev-2"}
    assert rows["ev-2"] == {
        "publisher": "Benzinga",
        "url": url,
        "evidence_id": "ev-2",
        "role": "SECONDARY",
    }


def test_review_defect_6_truncation_preserves_leading_whitespace() -> None:
    stored = "  Leading whitespace is verbatim. Second complete sentence. Third sentence."
    projected = project_public_answer(
        {
            "outcome": "ANSWERED",
            "key_sentence": {
                "text": stored,
                "publisher": "Benzinga",
                "url": "https://www.benzinga.com/news/whitespace",
            },
        }
    )

    shown = projected["key_sentence"]["text"]
    assert shown == "  Leading whitespace is verbatim. Second complete sentence."
    assert shown in stored


def test_review_defect_7_stream_requires_session_link_for_anonymous(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_stream_run(db_path, _snapshot())

    with TestClient(_public_client(db_path, monkeypatch)) as client:
        denied = client.get("/api/live-runs/run:stream/stream")

    cookie_name, cookie_value, token = _session_cookie()
    _link_session(db_path, "run:stream", day="2026-01-06", token=token)
    with TestClient(_public_client(db_path, monkeypatch)) as client:
        client.cookies.set(cookie_name, cookie_value)
        allowed = client.get("/api/live-runs/run:stream/stream")

    assert denied.status_code == 404
    assert denied.json() == {"detail": "run_not_found"}
    assert allowed.status_code == 200


def test_review_defect_8_admin_keeps_undeclared_answer_and_source_fields(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, "run:admin-open", snapshot=_snapshot(), visibility="private")

    with TestClient(_public_client(db_path, monkeypatch)) as client:
        response = client.get(
            "/api/live-runs/run:admin-open/view",
            headers={"Authorization": "Bearer test-admin-token"},
        )

    assert response.status_code == 200
    answer = response.json()["answer"]
    assert answer["excerpt_text"] == "SECRET_UNDECLARED_ANSWER_FIELD"
    assert answer["sources"][0]["claim_id"] == "private-claim-id"


def _payload_samples() -> dict[RunEventType, object]:
    return {
        RunEventType.RUN_ACCEPTED: RunAcceptedPayload(
            request_identity_digest="d" * 64,
            ticker="ACME",
            trade_date="2026-01-06",
            workflow_version="v1.1",
            model_provider_label="provider/model",
            stream_url="/api/live-runs/run:test/stream",
        ),
        RunEventType.STAGE_STARTED: StageStartedPayload(stage="research"),
        RunEventType.EVIDENCE_RETRIEVED: EvidenceRetrievedPayload(
            task_id="task", candidate_count=0
        ),
        RunEventType.EVIDENCE_ASSESSED: EvidenceAssessedPayload(
            status_ceiling="ABSTAIN",
            decision="ABSTAIN",
            normalization_diagnostics=(
                "QUOTE_OK:h1:hash:web_body:0001:One. Two. Three.",
                "primary_verifier_supported:reason:VERIFIER_MODEL_REASONING_SECRET",
            ),
        ),
        RunEventType.FOLLOWUP_STARTED: FollowUpStartedPayload(batch_id="batch"),
        RunEventType.ANSWER_STARTED: AnswerStartedPayload(
            writer_stream_id="writer",
            validated_claim_plan_ref="ref",
            validated_claim_plan_hash="a" * 64,
        ),
        RunEventType.ANSWER_COMPLETED: AnswerCompletedPayload(
            writer_stream_id="writer", final_text_hash="b" * 64
        ),
        RunEventType.ASSURANCE_COMPLETED: AssuranceCompletedPayload(valid=True),
        RunEventType.RUN_COMPLETED: RunCompletedPayload(
            result_status="SUCCEEDED",
            final_output_artifact_ref="artifact:answer",
            total_latency_ms=1,
            runtime_identity_ref="runtime",
        ),
        RunEventType.RUN_FAILED: RunFailedPayload(
            failure_code="FAILED", safe_message="Safe public failure."
        ),
        RunEventType.RUN_CANCELLED: RunCancelledPayload(
            cancellation_reason="USER", acknowledgement_mode="COOPERATIVE"
        ),
        RunEventType.FACTS_READY: FactsReadyPayload(
            window=FactsReadyWindow(
                information_window_start_at="2026-01-05T20:00:00Z",
                cutoff_at="2026-01-06T20:00:00Z",
                session_open_at="2026-01-06T14:30:00Z",
            ),
            facts={"target_return_pct": 1.0},
            headline="ACME moved.",
        ),
        RunEventType.RETRIEVAL_DONE: RetrievalDonePayload(
            inventory_count=1,
            included_in_pack=1,
            lead_only=0,
            duration_ms=1,
            items=(
                RetrievalDoneItem(
                    evidence_id="evidence",
                    publisher="Example Wire",
                    phase="SESSION",
                    content_state="FULL_TEXT",
                ),
            ),
        ),
        RunEventType.ANALYST_DONE: AnalystDonePayload(
            duration_ms=1,
            research_decision="ABSTAIN",
            evidence_decisions=(
                AnalystEvidenceDecision(
                    evidence_id="evidence",
                    disposition="IRRELEVANT",
                    reason_code="desk_not_selected",
                    note="MODEL_NOTE_SECRET",
                ),
            ),
            hypotheses=(
                AnalystHypothesis(
                    ref="h1", cause_type="COMPANY", proposed_role="PRIMARY"
                ),
            ),
        ),
        RunEventType.ANALYST_DECISION: AnalystDecisionPayload(
            kind="evidence",
            evidence_id="evidence",
            disposition="IRRELEVANT",
            reason_code="desk_not_selected",
            note="MODEL_NOTE_SECRET",
        ),
        RunEventType.CHECKS_DONE: ChecksDonePayload(
            duration_ms=1,
            quotes=(
                CheckedQuote(
                    hypothesis_ref="h1", evidence_id="evidence", rule="rule", kept=True
                ),
            ),
            gates=(
                CheckGate(gate="comove", result="passed", detail="RULE_DETAIL_SECRET"),
            ),
        ),
        RunEventType.GATES_DONE: GatesDonePayload(
            duration_ms=1,
            gates=(
                CheckGate(gate="comove", result="passed", detail="RULE_DETAIL_SECRET"),
            ),
        ),
        RunEventType.VERIFIER_DONE: VerifierDonePayload(
            called=True,
            verdict="SUPPORTED",
            category="COMPANY_NEWS",
            reason="VERIFIER_MODEL_REASONING_SECRET",
        ),
        RunEventType.ANSWER_READY: AnswerReadyPayload(
            answer_snapshot={
                "outcome": "ANSWERED",
                "sources": [],
                "primary": {
                    "quotes": [{
                        "text": "The company reported quarterly results.",
                        "publisher": "Issuer",
                        "url": "https://www.example.com/earnings",
                        "release_session": "intraday",
                        "release_time_et": "2026-11-02T11:10-05:00",
                        "private_timing": "must-not-escape",
                    }]
                },
            },
            provisional=True,
        ),
        RunEventType.TIER_DECIDED: TierDecidedPayload(
            tier="T3", reason_code="GAP_GE_2", reason_text="Tier reason."
        ),
        RunEventType.INVESTIGATOR_DONE: InvestigatorDonePayload(
            mode="agent_v3",
            stop_reason="model_stop",
            steps=[
                {
                    "tool": "search_news",
                    "status": "OK",
                    "summary": "INVESTIGATION_NOTE_SECRET",
                    "n_facts": 1,
                }
            ],
            evidence_added=1,
        ),
    }


def _event_with_diagnostic_fields(
    event_type: RunEventType, payload: object
) -> PublicRunEvent:
    return PublicRunEvent(
        schema_version="v1",
        event_id=None,
        run_id="run:test",
        sequence=1,
        event_type=event_type,
        emitted_at=datetime(2026, 1, 6, 21, tzinfo=timezone.utc),
        payload=payload,
    )


def test_public_event_allowlists_keep_only_declared_fields() -> None:
    samples = _payload_samples()
    assert set(samples) == set(_PUBLIC_EVENT_PAYLOAD_FIELDS)
    for event_type, payload in samples.items():
        event = _event_with_diagnostic_fields(event_type, payload)
        projected = project_public_event(event)
        assert projected is not None
        raw_keys = set(payload.model_dump())
        projected_keys = set(projected.payload.model_dump(mode="json"))
        expected = _PUBLIC_EVENT_PAYLOAD_FIELDS[event_type]
        assert projected_keys == raw_keys & expected
        text = json.dumps(projected.payload.model_dump(mode="json"), ensure_ascii=False)
        assert "QUOTE_OK" not in text
        assert "primary_verifier_supported" not in text
        assert "VERIFIER_MODEL_REASONING_SECRET" not in text
        assert "MODEL_NOTE_SECRET" not in text
        assert "RULE_DETAIL_SECRET" not in text
        assert "INVESTIGATION_NOTE_SECRET" not in text
        assert "normalization_diagnostics" not in text
        assert "diagnostic_leak" not in text
        if event_type is RunEventType.ANSWER_READY:
            quote = projected.payload.answer_snapshot["primary"]["quotes"][0]
            assert quote["release_session"] == "intraday"
            assert quote["release_time_et"] == "2026-11-02T11:10-05:00"
            assert "private_timing" not in quote

    delta = PublicRunEvent(
        schema_version="v1",
        event_id=None,
        run_id="run:test",
        sequence=1,
        event_type=RunEventType.ANSWER_DELTA,
        emitted_at=datetime(2026, 1, 6, 21, tzinfo=timezone.utc),
        payload=AnswerDeltaPayload(
            writer_stream_id="writer",
            delta_text="MODEL_DELTA_SECRET",
            delta_ordinal=1,
            cumulative_character_count=18,
        ),
    )
    assert project_public_event(delta) is None


def test_public_event_long_string_backstop_rejects_undeclared_fields() -> None:
    payload = EvidenceAssessedPayload(
        status_ceiling="ABSTAIN",
        decision="ABSTAIN",
        normalization_diagnostics=("QUOTE_OK:" + "x" * 250,),
    )
    event = PublicRunEvent(
        schema_version="v1",
        event_id=None,
        run_id="run:test",
        sequence=1,
        event_type=RunEventType.EVIDENCE_ASSESSED,
        emitted_at=datetime(2026, 1, 6, 21, tzinfo=timezone.utc),
        payload=payload,
    )
    violations = public_event_long_string_violations(event)
    assert violations
    assert "normalization_diagnostics" in violations[0]
def test_earnings_release_fields_are_in_public_source_allowlist() -> None:
    projected = project_public_answer({
        "outcome": "ANSWERED",
        "sources": [{
            "text": "The company reported quarterly results that exceeded expectations.",
            "publisher": "Issuer",
            "url": "https://www.example.com/earnings",
            "evidence_id": "sec:1",
            "release_session": "intraday",
            "release_time_et": "2026-11-02T11:10-05:00",
            "private_timing": "must-not-escape",
        }],
    })

    source = projected["sources"][0]
    assert source["release_session"] == "intraday"
    assert source["release_time_et"] == "2026-11-02T11:10-05:00"
    assert "private_timing" not in source


def test_session_news_shows_benzinga_summary_to_visitors_by_default(tmp_path: Path, monkeypatch) -> None:
    # 2026-11-18: the owner lifted the Benzinga/CNBC visitor limits; CATALYST_PUBLIC_SOURCE_LIMITS=1 restores them.
    monkeypatch.delenv("CATALYST_PUBLIC_SOURCE_LIMITS", raising=False)
    bodies, archive = _news_databases(tmp_path)
    monkeypatch.setenv("CATALYST_NEWS_BODIES_DB", str(bodies))
    monkeypatch.setenv("CATALYST_HEADLINE_ARCHIVE_DB", str(archive))
    monkeypatch.setenv("CATALYST_MEDIA_DB", str(tmp_path / "media.sqlite3"))
    monkeypatch.setenv("CATALYST_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("CATALYST_ADMIN_TOKEN", "test-admin-token")
    app = FastAPI()
    app.include_router(sessions.router)
    with TestClient(app) as client:
        public = client.get("/api/sessions/ACME/2026-01-06/news")
    items = {item["publisher"]: item for item in public.json()["items"]}
    assert items["Benzinga"]["summary"].startswith("BENZINGA FULL BODY")


def test_default_projection_keeps_benzinga_and_cnbc_quote_text(monkeypatch) -> None:
    from catalyst_app.public_projection import (
        _BenzingaBudget,
        public_projection_violations,
    )

    monkeypatch.delenv("CATALYST_PUBLIC_SOURCE_LIMITS", raising=False)
    budget = _BenzingaBudget()
    long_text = "One. Two. Three. Four."
    benzinga = budget.project({"publisher": "Benzinga", "url": "https://www.benzinga.com/x", "text": long_text})
    cnbc = budget.project({"publisher": "CNBC", "url": "https://www.cnbc.com/x", "text": long_text})
    assert benzinga["text"] == long_text and cnbc["text"] == long_text
    assert public_projection_violations({"sources": [benzinga, cnbc]}) == []
