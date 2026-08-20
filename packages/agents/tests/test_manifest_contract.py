"""V1.1 RunManifest contract tests (M2-7).

Agents-owned production run identity. It references DataRuntimeIdentity by
ref/hash and must not duplicate an independently recomputed data identity
(Final Migration TSD §14).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from catalyst_agents.runtime.manifest import RunManifest
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.trading_calendar import session_close_utc


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _temporal() -> TemporalIdentity:
    monday_close = _utc(session_close_utc("2026-01-05"))
    tuesday_close = _utc(session_close_utc("2026-01-06"))
    return TemporalIdentity(
        session_date="2026-01-06",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-06T14:30:00Z"),
        session_close_at=tuesday_close,
        information_window_start_at=monday_close,
        cutoff_at=tuesday_close,
    )


def _manifest(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "run:1",
        "request_hash": "e" * 64,
        "temporal_identity": _temporal(),
        "data_runtime_identity_ref": "runtime-id:7a004",
        "data_runtime_identity_hash": "f" * 64,
        "code_revision": "17693fa",
        "workflow_version": "v1.1",
        "policy_version": "p1",
        "analyst_model_id": "model-analyst",
        "analyst_prompt_hash": "a" * 64,
        "writer_model_id": "model-writer",
        "writer_prompt_hash": "b" * 64,
        "context_pack_schema_version": "v1",
        "packing_policy_version": "p1",
        "context_token_budget": 4000,
        "tokenizer_policy": "registered-bge-m3",
        "hypothesis_schema_version": "v1",
        "claim_schema_version": "v1",
        "max_corrective_rounds": 1,
        "max_actions_per_batch": 1,
        "run_timeout_seconds": 60,
        "provider_capability_revision": "cap:v1",
        "runtime_configuration": {"admission_slots": 4},
    }
    base.update(overrides)
    return base


def test_run_manifest_fields() -> None:
    manifest = RunManifest(**_manifest())
    assert set(RunManifest.model_fields) == {
        "run_id",
        "request_hash",
        "temporal_identity",
        "data_runtime_identity_ref",
        "data_runtime_identity_hash",
        "code_revision",
        "workflow_version",
        "policy_version",
        "analyst_model_id",
        "analyst_prompt_hash",
        "writer_model_id",
        "writer_prompt_hash",
        "context_pack_schema_version",
        "packing_policy_version",
        "context_token_budget",
        "tokenizer_policy",
        "hypothesis_schema_version",
        "claim_schema_version",
        "max_corrective_rounds",
        "max_actions_per_batch",
        "run_timeout_seconds",
        "provider_capability_revision",
        "runtime_configuration",
    }
    assert manifest.temporal_identity.session_date == "2026-01-06"


def test_run_manifest_production_freeze_defaults() -> None:
    manifest = RunManifest(**_manifest())
    assert manifest.max_corrective_rounds == 1
    assert manifest.max_actions_per_batch == 1
    assert manifest.run_timeout_seconds == 60


def test_run_manifest_hashes_are_hex_strings() -> None:
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(request_hash="not-hex"))
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(analyst_prompt_hash="x" * 63))
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(data_runtime_identity_hash="y" * 65))


def test_run_manifest_does_not_duplicate_data_runtime_identity() -> None:
    fields = set(RunManifest.model_fields)
    for duplicated in (
        "data_snapshot_id",
        "corpus_manifest_id",
        "fts_index_version",
        "dense_index_version",
        "embedding_model_revision",
        "reranker_revision",
        "query_policy_version",
    ):
        assert duplicated not in fields
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(data_snapshot_id="snapshot:7a004"))


def test_run_manifest_is_strict_and_frozen() -> None:
    manifest = RunManifest(**_manifest())
    with pytest.raises(ValidationError):
        manifest.run_id = "run:2"  # frozen
    with pytest.raises(ValidationError):
        RunManifest(**_manifest(), unknown_field=True)  # extra forbidden
