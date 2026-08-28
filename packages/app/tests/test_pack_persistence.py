"""RunArtifactsPackPersistence hardening (M6 corrective).

Before commit the production envelope verifies the declared pack/render
hashes against the canonical serializers, prompt-template identity against
the pack, and run_id against the store; round 1 and round 2 persist under
the actual pack round (never an event_seq-derived ordinal); mismatches are
rejected before anything becomes visible.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib

import pytest

from catalyst_app.persistence.connect import open_rw
from catalyst_app.persistence.events import EventRepository
from catalyst_app.persistence.schema import init_runtime_db
from catalyst_app.runtime.composition import RunArtifactsPackPersistence
from catalyst_agents.attribution.context_pack import (
    ContextBudget,
    EvidenceAnalystContextPack,
    TokenCountReport,
)
from catalyst_agents.attribution.context_pack_builder import (
    RenderMessage,
    canonical_context_pack_json,
)
from catalyst_agents.attribution.coverage import CoverageSummary
from catalyst_agents.attribution.move_profile import MoveProfile
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _temporal() -> TemporalIdentity:
    return TemporalIdentity(
        session_date="2026-01-06",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-06T14:30:00Z"),
        session_close_at=_utc("2026-01-06T21:00:00Z"),
        information_window_start_at=_utc("2026-01-05T21:00:00Z"),
        cutoff_at=_utc("2026-01-06T21:00:00Z"),
    )


def _runtime() -> DataRuntimeIdentity:
    return DataRuntimeIdentity(
        data_snapshot_id="s" * 64,
        corpus_manifest_id="m" * 64,
        fts_index_version="build:fts",
        dense_index_version="d" * 64,
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )


def _messages(round_no: int) -> tuple[RenderMessage, ...]:
    return (
        RenderMessage(role="system", content=f"observation=round-{round_no}"),
        RenderMessage(role="user", content="analyse"),
    )


def _rendered_sha(messages: tuple[RenderMessage, ...]) -> str:
    return hashlib.sha256(
        canonical_context_pack_json(
            [message.model_dump(mode="json") for message in messages]
        )
    ).hexdigest()


def _make_pack(*, run_id: str, round_no: int, template_version: str = "prompt:v1") -> EvidenceAnalystContextPack:
    messages = _messages(round_no)
    template_sha256 = hashlib.sha256(b"system: analyse\n").hexdigest()
    payload = {
        "schema_version": "1.0",
        "packing_policy_version": "pack:v1",
        "run_id": run_id,
        "round": round_no,
        "temporal_identity": _temporal(),
        "data_runtime_identity": _runtime(),
        "context_budget": ContextBudget(
            model_context_limit=8_000,
            reserved_output_tokens=300,
            reserved_system_instruction_tokens=200,
            observation_tokens=200,
            coverage_summary_tokens=200,
            research_history_tokens=100,
            inventory_tokens=200,
            evidence_payload_tokens=2_000,
            per_news_item_max_tokens=200,
            per_sec_chunk_max_tokens=250,
            lead_only_tokens=80,
            safety_margin_tokens=100,
        ),
        "token_count_report": TokenCountReport(
            tokenizer_identity="test",
            rendered_messages_tokens=10,
            reserved_output_tokens=300,
            safety_margin_tokens=100,
            remaining_payload_tokens=7_590,
        ),
        "observation": MoveProfile(target_return=2.5),
        "coverage_summary": CoverageSummary(
            eligible_item_count=1,
            eligible_asset_count=1,
            eligible_full_text_item_count=1,
            material_capable_item_count=1,
            material_capable_asset_count=1,
            primary_authority_asset_count=0,
            direct_primary_asset_count=1,
            reported_news_asset_count=1,
            commentary_lead_asset_count=0,
            unknown_role_asset_count=0,
            eligible_reported_news_group_count=1,
            unknown_independence_asset_count=0,
            known_duplicate_or_syndicated_asset_count=0,
            content_state_counts=(),
            parse_degraded_item_count=0,
        ),
        "context_pack_sha256": "0" * 64,
        "prompt_template_version": template_version,
        "prompt_template_sha256": template_sha256,
        "rendered_messages_sha256": _rendered_sha(messages),
    }
    # Validate the full payload with the placeholder hash, then compute the
    # canonical pack hash over the validated dump excluding the hash field
    # (identical to ContextPackFinalizer.finalize).
    context_pack_sha256 = hashlib.sha256(
        canonical_context_pack_json(
            EvidenceAnalystContextPack.model_validate(
                payload
            ).model_dump(mode="json", exclude={"context_pack_sha256"})
        )
    ).hexdigest()
    payload["context_pack_sha256"] = context_pack_sha256
    pack = EvidenceAnalystContextPack.model_validate(payload)
    assert pack.context_pack_sha256 == context_pack_sha256
    return pack


def _seed_run(db_path, run_id: str = "run:pack") -> None:
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at)"
            " VALUES (?, 'RUNNING', NULL, ?, ?, ?, 0, 't', 't')",
            (run_id, "a" * 64, f"manifest:{run_id}", "b" * 64),
        )
        conn.commit()


def _store(db_path, run_id: str = "run:pack") -> RunArtifactsPackPersistence:
    return RunArtifactsPackPersistence(
        db_path=db_path,
        events=EventRepository(db_path=db_path),
        run_id=run_id,
    )


def test_persist_uses_actual_pack_round_not_event_seq(tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path)
    store = _store(db_path)

    pack1 = _make_pack(run_id="run:pack", round_no=1)
    messages1 = _messages(1)
    refs1 = store.persist_pack_and_render(
        run_id="run:pack",
        pack=pack1,
        pack_sha256=pack1.context_pack_sha256,
        rendered_messages=messages1,
        rendered_messages_sha256=pack1.rendered_messages_sha256,
        prompt_template_version=pack1.prompt_template_version,
        prompt_template_sha256=pack1.prompt_template_sha256,
    )
    pack2 = _make_pack(run_id="run:pack", round_no=2)
    messages2 = _messages(2)
    refs2 = store.persist_pack_and_render(
        run_id="run:pack",
        pack=pack2,
        pack_sha256=pack2.context_pack_sha256,
        rendered_messages=messages2,
        rendered_messages_sha256=pack2.rendered_messages_sha256,
        prompt_template_version=pack2.prompt_template_version,
        prompt_template_sha256=pack2.prompt_template_sha256,
    )

    assert refs1.pack_artifact_id == "pack:run:pack:1"
    assert refs1.rendered_messages_artifact_id == "render:run:pack:1"
    assert refs2.pack_artifact_id == "pack:run:pack:2"
    assert refs2.rendered_messages_artifact_id == "render:run:pack:2"

    pair = store.load_pair(run_id="run:pack")
    assert pair is not None
    assert pair.pack.round == 2
    assert pair.refs.pack_artifact_id == "pack:run:pack:2"

    with open_rw(db_path) as conn:
        rows = conn.execute(
            "SELECT payload_json FROM run_events"
            " WHERE run_id = 'run:pack' AND event_type = 'stage.started'"
            " ORDER BY seq ASC"
        ).fetchall()
    rounds = [__import__("json").loads(r["payload_json"]).get("round") for r in rows]
    assert rounds == [1, 2]


def test_pack_sha256_mismatch_rejected_before_commit(tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path)
    store = _store(db_path)
    pack = _make_pack(run_id="run:pack", round_no=1)

    with pytest.raises(ValueError, match="pack_sha256"):
        store.persist_pack_and_render(
            run_id="run:pack",
            pack=pack,
            pack_sha256="f" * 64,  # wrong
            rendered_messages=_messages(1),
            rendered_messages_sha256=pack.rendered_messages_sha256,
            prompt_template_version=pack.prompt_template_version,
            prompt_template_sha256=pack.prompt_template_sha256,
        )
    with open_rw(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM run_artifacts WHERE run_id = 'run:pack'"
        ).fetchone()[0]
    assert count == 0


def test_rendered_messages_hash_mismatch_rejected(tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path)
    store = _store(db_path)
    pack = _make_pack(run_id="run:pack", round_no=1)

    with pytest.raises(ValueError, match="rendered_messages_sha256"):
        store.persist_pack_and_render(
            run_id="run:pack",
            pack=pack,
            pack_sha256=pack.context_pack_sha256,
            rendered_messages=_messages(1),
            rendered_messages_sha256="e" * 64,  # wrong
            prompt_template_version=pack.prompt_template_version,
            prompt_template_sha256=pack.prompt_template_sha256,
        )
    with open_rw(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM run_artifacts WHERE run_id = 'run:pack'"
        ).fetchone()[0]
    assert count == 0


def test_prompt_template_identity_mismatch_rejected(tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path)
    store = _store(db_path)
    pack = _make_pack(run_id="run:pack", round_no=1)

    with pytest.raises(ValueError, match="prompt-template"):
        store.persist_pack_and_render(
            run_id="run:pack",
            pack=pack,
            pack_sha256=pack.context_pack_sha256,
            rendered_messages=_messages(1),
            rendered_messages_sha256=pack.rendered_messages_sha256,
            prompt_template_version="prompt:wrong",
            prompt_template_sha256=pack.prompt_template_sha256,
        )
    with open_rw(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM run_artifacts WHERE run_id = 'run:pack'"
        ).fetchone()[0]
    assert count == 0


def test_run_id_mismatch_rejected(tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_run(db_path, run_id="run:pack")
    store = _store(db_path, run_id="run:pack")
    pack = _make_pack(run_id="run:pack", round_no=1)

    with pytest.raises(ValueError, match="store run_id"):
        store.persist_pack_and_render(
            run_id="run:other",
            pack=pack,
            pack_sha256=pack.context_pack_sha256,
            rendered_messages=_messages(1),
            rendered_messages_sha256=pack.rendered_messages_sha256,
            prompt_template_version=pack.prompt_template_version,
            prompt_template_sha256=pack.prompt_template_sha256,
        )
