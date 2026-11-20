from __future__ import annotations

import json
from pathlib import Path

from catalyst_app.api_dto import RunViewDTO
from catalyst_app.main import create_app
from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.schema import init_runtime_db
from fastapi.testclient import TestClient


def _seed_run(
    db_path: Path,
    run_id: str,
    *,
    lifecycle: str = "COMPLETED",
    artifacts: dict[str, object] | None = None,
    failure: tuple[str, str] | None = None,
    created_at: str = "2026-01-06T21:00:00Z",
    finished_at: str | None = "2026-01-06T21:00:03Z",
) -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, request_hash, run_manifest_id,"
            " manifest_hash, capacity_slot, created_at, updated_at, ticker, provider,"
            " failure_code, failure_message) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
            (run_id, lifecycle, "a" * 64, f"manifest:{run_id}", "b" * 64,
             created_at, finished_at or created_at, "AAPL", "openai",
             failure[0] if failure else None, failure[1] if failure else None),
        )
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage,"
            " payload_json, schema_version) VALUES (?, 1, ?, 'run.accepted', 'ADMISSION', '{}', 'v1')",
            (run_id, created_at),
        )
        seq = 1
        if finished_at:
            seq = 2
            terminal_type = "run.failed" if lifecycle == "FAILED" else "run.completed"
            conn.execute(
                "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage,"
                " payload_json, schema_version) VALUES (?, ?, ?, ?, 'TERMINAL', '{}', 'v1')",
                (run_id, seq, finished_at, terminal_type),
            )
        for index, (artifact_type, payload) in enumerate((artifacts or {}).items()):
            conn.execute(
                "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type,"
                " payload_hash, payload_json, optional) VALUES (?, ?, ?, ?, ?, ?, 0)",
                (f"{artifact_type}:{run_id}:{index}", run_id, 1,
                 artifact_type, "c" * 64, json.dumps(payload)),
            )
        conn.commit()


def _manifest(*, with_policy: bool = True) -> dict[str, object]:
    manifest: dict[str, object] = {
        "run_id": "unused",
        "code_revision": "abc123",
        "analyst_model_id": "deepseek-chat",
        "temporal_identity": {"session_date": "2026-01-06"},
    }
    if with_policy:
        manifest["attribution_policy"] = {"verifier_enabled": True, "force_full": False}
    return manifest


def _get_view(db_path: Path, run_id: str = "run:view"):
    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        return client.get(f"/api/live-runs/{run_id}/view")



def _stored(answer):
    """The answer as stored: the view adds product_tier and group_context (catalyst_app.product_tier)."""
    data = answer.model_dump(mode="python") if hasattr(answer, "model_dump") else dict(answer)
    data.pop("product_tier", None)
    data.pop("group_context", None)
    return data

def test_completed_answered_run_view_uses_persisted_snapshot_trace_and_cost(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    snapshot = {"schema_version": "answer_snapshot_v1", "tier": "T3", "outcome": "ANSWERED",
                "key_sentence": {"text": "Checked sentence", "evidence_id": "ev-1"}, "facts": {"return": 2.1}}
    trace = {"schema_version": "decision_trace_v1", "model_calls": {"analyst": 1}, "total_ms": 1250}
    diagnostics = {"provider": {"cost_usd": 0.002, "cost_method": "provider_reported",
                                 "analyst_logical_calls": 1, "analyst_provider_attempts": 2,
                                 "writer_logical_calls": 1, "writer_provider_attempts": 1,
                                 "verifier_logical_calls": 1, "verifier_provider_attempts": 1}}
    _seed_run(db_path, "run:view", artifacts={"run_manifest": _manifest(), "answer_snapshot": snapshot,
        "decision_trace": trace, "run_diagnostics": diagnostics})

    response = _get_view(db_path)

    assert response.status_code == 200
    view = RunViewDTO.model_validate(response.json())
    assert _stored(view.answer) == snapshot
    assert view.answer["product_tier"] == "CONFIRMED"
    assert view.trace == trace
    assert view.run.duration_ms == 1250
    assert view.run.duration_source == "decision_trace"
    assert view.cost.model_calls == {"analyst": 1, "writer": 1, "verifier": 1}
    assert view.cost.provider_attempts == 4
    assert view.cost.cost_usd == 0.002


def test_tier_only_and_abstained_snapshots_are_returned_verbatim(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    tier_snapshot = {"schema_version": "answer_snapshot_v1", "tier": "T1", "outcome": "TIER_ONLY",
                     "run_id": "run:tier", "ticker": "AAPL", "session_date": "2026-01-06",
                     "status": "ABSTAIN", "headline": "AAPL in line", "tier_reason": "No material move.",
                     "facts": {"return": 0.4}, "window": {
                         "information_window_start_at": "2026-01-05T21:00:00Z",
                         "cutoff_at": "2026-01-06T21:00:00Z",
                         "session_open_at": "2026-01-06T14:30:00Z",
                         "future_snapshot_field": "preserved",
                     }, "limitations": [], "also_sentence": None, "governing_ref": None}
    abstained = {"schema_version": "answer_snapshot_v1", "tier": "T3", "outcome": "ABSTAINED",
                 "abstention": {"reason_code": "NO_QUOTE", "leads": []}}
    _seed_run(db_path, "run:tier", artifacts={"run_manifest": _manifest(), "answer_snapshot": tier_snapshot})
    _seed_run(db_path, "run:abstain", artifacts={"run_manifest": _manifest(), "answer_snapshot": abstained})

    tier_response = _get_view(db_path, "run:tier")
    tier = RunViewDTO.model_validate(tier_response.json())
    abstain = RunViewDTO.model_validate(_get_view(db_path, "run:abstain").json())

    assert _stored(tier.answer) == tier_snapshot
    assert _stored(tier_response.json()["answer"]) == tier_snapshot
    assert tier.answer["product_tier"] == "ORDINARY_MOVE"
    assert abstain.answer["product_tier"] == "NOTHING_FOUND"
    assert tier.answer["outcome"] == "TIER_ONLY"
    assert abstain.answer["abstention"].reason_code is None
    assert abstain.availability["abstention_reason"] == "not_recorded"


def test_answer_snapshot_without_new_optional_fields_stays_unchanged(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    snapshot = {"outcome": "ANSWERED", "headline": "AAPL moved",
                "key_sentence": {"text": "Checked sentence"}}
    _seed_run(db_path, "run:legacy-answer", artifacts={"run_manifest": _manifest(),
        "answer_snapshot": snapshot})

    response = _get_view(db_path, "run:legacy-answer")

    assert response.status_code == 200
    assert _stored(response.json()["answer"]) == snapshot
    assert "tier" not in response.json()["answer"]


def test_live_run_history_additive_fields_filters_and_summary(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    answered = {"outcome": "ANSWERED", "headline": "AAPL moved", "facts": {"target_return_pct": 2.5},
                "key_sentence": {"text": " Apple rose after its results beat estimates. "}}
    manifest = {**_manifest(), "provider": "openai"}
    diagnostics = {"provider": {"cost_usd": 0.012, "cost_method": "reported"}}
    _seed_run(db_path, "run:history", artifacts={"run_manifest": manifest, "answer_snapshot": answered,
                                                  "run_diagnostics": diagnostics})
    _seed_run(db_path, "run:tier", artifacts={"run_manifest": manifest,
                                               "answer_snapshot": {"outcome": "TIER_ONLY"}})
    _seed_run(db_path, "run:failed", lifecycle="FAILED", artifacts={"run_manifest": manifest})
    _seed_run(db_path, "run:cancelled", lifecycle="CANCELLED", artifacts={"run_manifest": manifest})
    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        all_runs = client.get("/api/live-runs")
        filtered = client.get("/api/live-runs?outcome=ANSWERED")
        failed = client.get("/api/live-runs?outcome=FAILED")
        cancelled = client.get("/api/live-runs?outcome=CANCELLED")
        tier = client.get("/api/live-runs?outcome=TIER_ONLY")
        paged = client.get("/api/live-runs?limit=1")
        invalid = client.get("/api/live-runs?outcome=OTHER")
    assert all_runs.status_code == 200
    payload = all_runs.json()
    history_item = next(item for item in payload["items"] if item["run_id"] == "run:history")
    assert history_item["move_pct"] == 2.5
    assert history_item["headline"] == "AAPL moved"
    assert history_item["model"] == "openai/deepseek-chat"
    assert history_item["cost_usd"] == 0.012
    assert history_item["cause"] == "Apple rose after its results beat estimates."
    assert history_item["cost_method"] == "reported"
    tier_item = next(item for item in payload["items"] if item["run_id"] == "run:tier")
    assert tier_item["cause"] is None and tier_item["cost_method"] is None
    assert payload["summary"] == {"runs": 4, "answered": 1, "spent_usd": 0.012, "matching": 4}
    assert len(filtered.json()["items"]) == 1
    assert failed.json()["items"][0]["outcome"] == "FAILED"
    assert cancelled.json()["items"][0]["outcome"] == "CANCELLED"
    assert tier.json()["items"][0]["outcome"] == "TIER_ONLY"
    assert len(paged.json()["items"]) == 1
    assert paged.json()["summary"]["runs"] == 4
    assert invalid.status_code == 422


def test_legacy_abstention_reason_is_derived_from_trace_or_marked_unavailable(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    snapshot = {"schema_version": "answer_snapshot_v1", "outcome": "ABSTAINED",
                "abstention": {"leads": []}}
    trace = {"schema_version": "decision_trace_v1", "final_status": "ABSTAIN",
             "retrieval": {"evidence_in_state": 2}, "rounds": [{
                 "model_reported": {"analyst": {"hypotheses": []},
                                    "verifier": {"called": False, "skipped_because": "no_primary"}},
                 "code_checks": {"quotes": []},
             }]}
    _seed_run(db_path, "run:derived", artifacts={"run_manifest": _manifest(),
        "answer_snapshot": snapshot, "decision_trace": trace})
    _seed_run(db_path, "run:unknown", artifacts={"run_manifest": _manifest(),
        "answer_snapshot": snapshot})

    derived = RunViewDTO.model_validate(_get_view(db_path, "run:derived").json())
    unknown = RunViewDTO.model_validate(_get_view(db_path, "run:unknown").json())

    assert derived.availability["abstention_reason"] == "derived"
    assert derived.answer.abstention.reason_code == "NO_PRIMARY_PROPOSED"
    assert unknown.availability["abstention_reason"] == "not_recorded"
    schema = RunViewDTO.model_json_schema()
    assert "NO_STOCK_NEWS" in str(schema)


def test_web_source_supported_outcome_is_typed_and_available(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    snapshot = {"schema_version": "answer_snapshot_v1", "outcome": "WEB_SOURCE_SUPPORTED",
                "headline": "TSLA fell 3.56%.", "key_sentence": {"text": "Tesla raised guidance."},
                "sources": [{"url": "https://news.example/tesla"}]}
    _seed_run(db_path, "run:web-source", artifacts={"run_manifest": _manifest(),
        "answer_snapshot": snapshot})

    response = _get_view(db_path, "run:web-source")

    assert response.status_code == 200
    view = RunViewDTO.model_validate(response.json())
    assert view.answer.outcome == "WEB_SOURCE_SUPPORTED"
    assert view.availability["answer"] == "present"
    assert "WEB_SOURCE_SUPPORTED" in str(RunViewDTO.model_json_schema())


def test_failed_run_reports_safe_failure_and_missing_trace_state(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, "run:view", lifecycle="FAILED", failure=("PROVIDER_ERROR", "x" * 400))

    response = _get_view(db_path)

    assert response.status_code == 200
    view = RunViewDTO.model_validate(response.json())
    assert view.run.lifecycle == "FAILED"
    assert view.failure.code == "PROVIDER_ERROR"
    assert len(view.failure.message) == 300
    assert view.trace is None
    assert view.availability["trace"] == "not_recorded"


def test_old_shape_run_does_not_invent_snapshot_trace_or_policy(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, "run:view", artifacts={"run_manifest": _manifest(with_policy=False),
                                              "answer": {"text": "legacy rendered answer"}})

    response = _get_view(db_path)

    assert response.status_code == 200
    view = RunViewDTO.model_validate(response.json())
    assert view.answer is None
    assert view.legacy_text == "legacy rendered answer"
    assert view.trace is None
    assert view.run.policy is None
    assert view.availability["answer"] == "not_recorded"
    assert view.availability["trace"] == "not_recorded"
    assert view.availability["policy"] == "not_recorded"


def test_cost_unavailable_is_null_with_recorded_method_and_unknown_run_is_404(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    diagnostics = {"provider": {"cost_usd": None, "cost_method": "unavailable",
                                 "analyst_logical_calls": 0, "analyst_provider_attempts": 0,
                                 "writer_logical_calls": 0, "writer_provider_attempts": 0,
                                 "verifier_logical_calls": 0, "verifier_provider_attempts": 0}}
    _seed_run(db_path, "run:view", artifacts={"run_manifest": _manifest(), "run_diagnostics": diagnostics})

    response = _get_view(db_path)
    missing = _get_view(db_path, "missing")

    assert response.status_code == 200
    view = RunViewDTO.model_validate(response.json())
    assert view.cost.cost_usd is None
    assert view.cost.cost_method == "unavailable"
    assert view.availability["cost"] == "present"
    assert missing.status_code == 404


def test_live_run_history_splits_reported_from_no_cause(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    manifest = {**_manifest(), "provider": "openai"}
    reported = {"outcome": "ABSTAINED", "facts": {"target_return_pct": 5.0},
                "reported_reason": {"quote": "Goldman was picked to lead the IPO.",
                                    "cause": "chosen to lead the SpaceX IPO"}}
    _seed_run(db_path, "run:reported", artifacts={"run_manifest": manifest, "answer_snapshot": reported})
    _seed_run(db_path, "run:none", artifacts={"run_manifest": manifest,
                                               "answer_snapshot": {"outcome": "ABSTAINED"}})
    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        only_reported = client.get("/api/live-runs?outcome=REPORTED").json()
        no_cause = client.get("/api/live-runs?outcome=NO_CAUSE").json()
        abstained = client.get("/api/live-runs?outcome=ABSTAINED").json()
    assert [item["run_id"] for item in only_reported["items"]] == ["run:reported"]
    assert only_reported["items"][0]["cause"] == "Chosen to lead the SpaceX IPO"
    assert only_reported["items"][0]["product_tier"] == "REPORTED"
    assert only_reported["summary"]["matching"] == 1
    assert [item["run_id"] for item in no_cause["items"]] == ["run:none"]
    assert no_cause["items"][0]["cause"] is None
    assert no_cause["summary"]["matching"] == 1
    assert len(abstained["items"]) == 2
