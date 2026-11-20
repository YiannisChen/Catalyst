"""The API contract's JSON samples validate against contracts/api_schema.

Samples and schema are documentation-first artefacts; this test keeps them from drifting apart, and the schema is
meant to be reused by the real implementation.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
SCHEMA = json.loads((CONTRACTS / "api_schema/contract_v1.schema.json").read_text())
SAMPLES = CONTRACTS / "api_samples"


def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator({"$schema": SCHEMA["$schema"], "$ref": f"#/$defs/{name}", "$defs": SCHEMA["$defs"]})


def check(name: str, instance) -> None:
    errors = sorted(validator(name).iter_errors(instance), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{name} {list(e.path)}: {e.message}" for e in errors[:5])


def sample(file: str) -> dict:
    return json.loads((SAMPLES / file).read_text())


def test_schema_itself_is_valid():
    Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("file", ["01_answered_reported_attribution.json", "02_abstained_possible_explanation.json",
                                   "03_earnings_fallback.json"])
def test_answer_samples(file):
    check("AnswerSnapshotV2", sample(file)["answer_snapshot"])


def test_failed_run_events_and_view():
    s = sample("06_failed_run_answer_invalidated.json")
    for ev in s["events"]:
        check("PublicRunEvent", ev)
    check("RunViewV2", s["view_after"])


def test_featured_case():
    s = sample("07_featured_case_replay.json")
    check("FeaturedItem", s["list_item"])
    check("RunViewV2", s["detail"]["view"])
    check("FeaturedReplay", s["replay"])
    assert [e["event"]["sequence"] for e in s["replay"]["events"]] == sorted(e["event"]["sequence"] for e in s["replay"]["events"])
    assert set(s["categories"]) <= set(SCHEMA["$defs"]["FeaturedCategory"]["enum"])


def test_invite_run():
    s = sample("08_invite_run.json")
    check("RunAccepted", s["accepted_response"]["body"])
    check("Quota", s["quota_response"]["body"])


def test_no_sample_carries_full_text_prompts_or_secrets():
    blob = "".join(p.read_text() for p in SAMPLES.glob("*.json")).lower()
    for needle in ("api_key", "sk-", "prompt", "llm_exchange"):
        assert needle not in blob, needle


def test_schema_rejects_known_contract_violations():
    base = sample("01_answered_reported_attribution.json")["answer_snapshot"]

    wrong_scope = copy.deepcopy(base); wrong_scope["scope_note"] = "结论只到事件层面，不判定该事件的影响大小。"
    no_basis = copy.deepcopy(base); del no_basis["basis_text"]
    disables = copy.deepcopy(base)
    disables["data_gaps"] = [{"source": "target_news", "status": "missing", "disables": ["unique_company_event"], "note": "x"}]
    for bad in (wrong_scope, no_basis, disables):
        assert list(validator("AnswerSnapshotV2").iter_errors(bad)), "violation not caught"

    abst = sample("02_abstained_possible_explanation.json")["answer_snapshot"]
    gate_code_in_reason = copy.deepcopy(abst); gate_code_in_reason["abstention"]["reason_code"] = "NO_ELIGIBLE_CAUSE"
    assert list(validator("AnswerSnapshotV2").iter_errors(gate_code_in_reason))

    verifier = {"schema_version": "v1", "run_id": "r", "sequence": 1, "event_type": "verifier.done",
                "emitted_at": "2026-10-19T09:00:00Z", "payload": {"called": True, "reason": "model free text"}}
    assert list(validator("PublicRunEvent").iter_errors(verifier))

    tavily = {"schema_version": "v1", "run_id": "r", "sequence": 1, "event_type": "run.failed", "emitted_at": "2026-10-19T09:00:00Z",
              "payload": {"failure": {"code": "TAVILY_BUDGET_EXCEEDED"}, "provisional_output_invalidated": True}}
    assert list(validator("PublicRunEvent").iter_errors(tavily))

    failed_with_answer = sample("06_failed_run_answer_invalidated.json")["view_after"]
    failed_with_answer = copy.deepcopy(failed_with_answer); failed_with_answer["answer"] = base
    assert list(validator("RunViewV2").iter_errors(failed_with_answer))


def _run_event(event_type: str, payload: dict) -> dict:
    return {
        "schema_version": "v1",
        "run_id": "r",
        "sequence": 1,
        "event_type": event_type,
        "emitted_at": "2026-10-23T09:00:00Z",
        "payload": payload,
    }


def test_answer_ready_is_always_provisional():
    # `answer.ready` is ALWAYS provisional; finality comes only from a
    # persisted COMPLETED lifecycle plus its run.completed event.
    snap = copy.deepcopy(sample("01_answered_reported_attribution.json")["answer_snapshot"])
    snap["run_id"] = "r"
    provisional = _run_event("answer.ready", {"provisional": True, "answer_snapshot": snap})
    check("PublicRunEvent", provisional)

    final = copy.deepcopy(provisional)
    final["payload"]["provisional"] = False
    assert list(validator("PublicRunEvent").iter_errors(final)), \
        "provisional=false answer.ready accepted"


def test_featured_replay_answer_ready_is_provisional():
    replay = sample("07_featured_case_replay.json")["replay"]
    ready_events = [item["event"] for item in replay["events"]
                    if item["event"]["event_type"] == "answer.ready"]
    assert ready_events, "featured replay has no answer.ready event"
    for event in ready_events:
        assert event["payload"]["provisional"] is True, \
            "featured replay answer.ready is not provisional"
        check("PublicRunEvent", event)


def test_run_failed_event_matches_app_failure_code_shape():
    # run.failed payload aligns with the merged app code
    # (events.RunFailedPayload): top-level `failure_code`, not nested
    # `failure.code`. Query DTO keeps `failure.code` unchanged.
    ev = _run_event("run.failed", {"failure_code": "TIMEOUT", "stage": "EXECUTION",
                                   "retryable": False, "safe_message": "Run failed.",
                                   "provisional_output_invalidated": True})
    check("PublicRunEvent", ev)


def test_run_failed_event_rejects_query_failure_shape():
    ev = _run_event("run.failed", {"failure": {"code": "TIMEOUT"}, "provisional_output_invalidated": True})
    assert list(validator("PublicRunEvent").iter_errors(ev)), \
        "run.failed accepted nested query failure.code shape"


def test_run_failed_rejects_code_outside_contract_enum():
    bad = _run_event("run.failed", {"failure_code": "TAVILY_BUDGET_EXCEEDED", "stage": "EXECUTION",
                                    "retryable": False, "safe_message": "Run failed.",
                                    "provisional_output_invalidated": True})
    assert list(validator("PublicRunEvent").iter_errors(bad)), \
        "non-contract failure_code accepted"


def test_run_view_keeps_query_failure_code_shape():
    view = copy.deepcopy(sample("06_failed_run_answer_invalidated.json")["view_after"])
    check("RunViewV2", view)

    bad = copy.deepcopy(view)
    bad["failure_code"] = bad.pop("failure")["code"]
    assert list(validator("RunViewV2").iter_errors(bad)), \
        "RunViewV2 accepted event-style top-level failure_code"


def test_data_gap_accepts_supplementary_news_category():
    gap = {"source": "supplementary_news", "status": "missing", "disables": [],
           "note": "补充新闻没有取到"}
    check("DataGap", gap)


def test_data_gap_rejects_provider_group_as_public_source():
    bad = {"source": "benzinga_kitco", "status": "missing", "disables": [],
           "note": "补充新闻没有取到"}
    assert list(validator("DataGap").iter_errors(bad)), \
        "DataGap accepted provider group instead of public source category"




def test_first_version_rejects_backfill_and_follow_up_states():
    run = {
        "run_id": "run-1",
        "lifecycle_status": "BACKFILL_PENDING",
        "created_at": "2026-10-23T09:00:00Z",
    }
    assert list(validator("RunObjectV1").iter_errors(run)), \
        "first-version run response accepted a backfill lifecycle status"

    view = {
        "schema_version": "run_view_v2",
        "run": {
            "run_id": "run-1",
            "lifecycle": "COMPLETED",
            "follow_up": {"status": "scheduled"},
        },
        "answer": None,
        "availability": {},
    }
    assert list(validator("RunViewV2").iter_errors(view)), \
        "first-version view response accepted a follow-up state"

    event = {
        "schema_version": "v1",
        "run_id": "run-1",
        "sequence": 1,
        "event_type": "run.completed",
        "emitted_at": "2026-10-23T09:00:00Z",
        "payload": {"follow_up": {"status": "scheduled"}},
    }
    assert list(validator("PublicRunEvent").iter_errors(event)), \
        "first-version completion event accepted a follow-up state"
