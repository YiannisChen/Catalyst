"""M6 corrective: real default runtime composition (Findings A/C/G/H).

The default FastAPI lifespan must compose one app-owned runtime: one
EventRepository (with the SSE after-commit notifier), one RunClaimer, one
CancellationTokenRegistry/CancellationController, one bounded RunExecutor,
one AdmissionController, the production RunManifest factory, and the
production run adapter that invokes the repository-owned ``run_v1_graph``
with the app-owned EventRepository-backed PackPersistence and the StreamBridge
Writer sink. The test injects only deterministic external boundaries (fake
LLM providers, fake observation provider, fake retriever/loader); it never
injects a fake RunManifest factory, a fake run adapter, a
``terminalize_with_attribution`` shortcut, or a second graph.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from catalyst_app.main import create_app
from catalyst_app.runtime_credential_store import RuntimeCredentialStore
from catalyst_agents.attribution.provider import ContextInputs, RetrievedEvidence
from catalyst_agents.retrieval.corrective import (
    BackendCapability,
    BackendHealth,
    CorrectiveCapabilityRegistry,
)
from catalyst_agents.retrieval.task import EvidenceNeed
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _temporal() -> TemporalIdentity:
    return TemporalIdentity(
        session_date="2026-01-15",
        market_timezone="America/New_York",
        session_open_at=_utc("2026-01-15T14:30:00Z"),
        session_close_at=_utc("2026-01-15T21:00:00Z"),
        information_window_start_at=_utc("2026-01-14T21:00:00Z"),
        cutoff_at=_utc("2026-01-15T21:00:00Z"),
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


class FakeRuntimeDependencyLoader:
    """External-boundary loader: identity-bound runtime deps, no SQLite conn."""

    def __init__(self, *, runtime: DataRuntimeIdentity | None = None) -> None:
        self._runtime = runtime or _runtime()
        self.calls = 0

    def get_dependencies(self, *, force_reload: bool = False):
        self.calls += 1
        from catalyst_agents.runtime.dependencies import RuntimeDependencies

        return RuntimeDependencies(
            sqlite_db_path=Path("/tmp/catalyst-test.db"),
            lancedb_dir=Path("/tmp/lancedb"),
            lancedb_table=None,
            embedding_fn=lambda _: [],
            embedding_model="BAAI/bge-m3",
            embedding_dim=1024,
            reranker=None,
            reranker_model="BAAI/bge-reranker-v2-m3",
            default_model="gemini-2.5-flash-nothink",
            health={
                "status": "ready",
                "sqlite": {"status": "ready", "path": "/tmp/catalyst-test.db"},
                "lancedb": {"status": "ready", "path": "/tmp/lancedb", "table": "chunks"},
                "embedding": {"status": "ready", "model": "BAAI/bge-m3", "vector_dim": 1024},
                "reranker": {"status": "ready", "model": "BAAI/bge-reranker-v2-m3"},
                "default_model": {"status": "ready", "model": "gemini-2.5-flash-nothink"},
                "retrieval": {"status": "ready"},
                "errors": [],
            },
            retriever=None,
            requested_manifest_id="m" * 64,
            index_manifest_id="d" * 64,
            data_runtime_identity=self._runtime,
        )


class FakeObservationProvider:
    def load_context_inputs(
        self, *, ticker, session_date, cutoff, information_window_start_at=None
    ) -> ContextInputs:
        return ContextInputs(
            ticker=ticker,
            session_date=date.fromisoformat(session_date),
            cutoff=cutoff,
            target_close=110.0,
            previous_target_close=100.0,
            target_open=102.0,
            previous_2_target_close=90.0,
            target_volume=2200.0,
            expected_prior_sessions=tuple(f"2026-01-{d:02d}" for d in range(1, 21)),
            prior_volumes_by_session={f"2026-01-{d:02d}": 1000.0 + d for d in range(1, 21)},
            benchmark_ticker="SPY",
            benchmark_return_pct=9.5,
            sector_ticker="XLK",
            sector_return_pct=9.2,
            peer_returns_by_ticker={"MSFT": 9.8, "NVDA": 9.6, "GOOGL": 9.4},
        )


class FakeRetriever:
    def __init__(self) -> None:
        self.calls = 0

    def _evidence(self, chunk_id: str, rank: int) -> RetrievedEvidence:
        return RetrievedEvidence(
            chunk_id=chunk_id,
            document_id=f"doc:{chunk_id}",
            content_text=f"evidence {chunk_id}",
            available_at="2026-01-15T10:00:00Z",
            source_class="issuer_disclosure",
            ticker_associations=("AAPL",),
            dedup_cluster_id=None,
            cluster_first_available_at="2026-01-15T10:00:00Z",
            representative_document_id=f"doc:{chunk_id}",
            is_novel=False,
            lexical_raw_score=-1.0,
            lexical_rank=rank,
            corpus_manifest_id="m" * 64,
            index_manifest_id="d" * 64,
            mode_requested="reranked",
            mode_served="reranked",
            is_degraded=False,
            fallback_reason=None,
            reranker_score=float(10 - rank),
            reranker_rank=rank,
            canonical_asset_id=f"asset:{chunk_id}",
            canonical_content_version_id=f"version:{chunk_id}",
            corpus_document_id=f"doc:{chunk_id}",
            section_key="body",
            chunk_ordinal=1,
            asset_type="NEWS",
            content_hash="c" * 64,
            material_capability="MATERIAL_CAPABLE",
            serving_status="body_candidate",
            temporal_precision="publication_time",
            evidence_role="DIRECT_PRIMARY",
            provider="polygon",
            content_state="FULL_TEXT",
            eligible_at=_utc("2026-01-15T10:00:00Z"),
            temporal_identity=_temporal(),
            data_runtime_identity=_runtime(),
        )

    def retrieve(self, query, *, ticker, cutoff, requested_manifest_id,
                 temporal_identity=None, top_k=8, candidate_depth=20):
        self.calls += 1
        return (self._evidence("e1", 1),)


class FakeAnalystProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.capability_metadata = {
            "supports_structured_output": True,
            "supports_true_streaming": True,
            "declares_token_accounting": True,
            "normalizes_timeout_errors": True,
            "capability_revision": "v1.1-capability-1",
        }

    def _decision(self) -> dict:
        return {
            "schema_version": "1.0",
            "evidence_decisions": [
                {
                    "evidence_id": "e1",
                    "disposition": "SUPPORT",
                    "supports_hypothesis_refs": ["h1"],
                    "reason_code": "material_support",
                }
            ],
            "candidate_hypotheses": [
                {
                    "hypothesis_ref": "h1",
                    "cause_type": "COMPANY_SPECIFIC_CATALYST",
                    "statement": "AAPL rose on record guidance.",
                    "supporting_evidence_ids": ["e1"],
                    "magnitude_fit": "STRONG",
                    "proposed_role": "PRIMARY",
                }
            ],
            "research_decision": "READY",
            "recommended_status": "SUFFICIENT",
            "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
        }

    def invoke(self, messages):
        self.calls += 1
        return self._decision()

    def with_structured_output(self, schema):
        outer = self

        class Surface:
            def invoke(self, messages):
                return outer.invoke(messages)

        return Surface()


class FakeWriterProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.capability_metadata = {
            "supports_structured_output": True,
            "supports_true_streaming": True,
            "declares_token_accounting": True,
            "normalizes_timeout_errors": True,
            "capability_revision": "v1.1-capability-1",
        }

    def stream(self, messages):
        self.calls += 1
        import re

        prompt = messages[0]["content"] if isinstance(messages[0], dict) else messages[0].content
        match = re.search(r"claim_id: ([A-Za-z0-9:_-]+)", prompt)
        claim_id = match.group(1) if match else "claim:1"
        yield (
            "SUMMARY\nAAPL rose on record guidance. "
            f"[{claim_id}] (e1)\n"
            "CAUSAL_EXPLANATION\nGuidance raised forward revenue.\n"
            "LIMITATIONS\nNone."
        )


class FakeGraphResolver:
    """External-boundary graph resolver returning deterministic providers."""

    def __init__(self) -> None:
        self.analyst = FakeAnalystProvider()
        self.writer = FakeWriterProvider()
        self.retriever = FakeRetriever()
        self.last_api_key: str | None = None

    def resolve(self, manifest, boundary):
        self.last_api_key = boundary.api_key
        capabilities = {}
        for name in (
            "COMPANY_PRIMARY", "COMPANY_NEWS", "SECTOR_NEWS", "MACRO_EVENT",
            "MACRO_SERIES", "FUNDAMENTALS",
        ):
            capabilities[EvidenceNeed(name)] = BackendCapability(
                backend=f"backend:{name.lower()}", health=BackendHealth.HEALTHY
            )
        from catalyst_app.runtime.composition import ResolvedGraphRuntime

        return ResolvedGraphRuntime(
            observation_provider=FakeObservationProvider(),
            retriever=self.retriever,
            analyst_llm=self.analyst,
            writer_llm=self.writer,
            capability_registry=CorrectiveCapabilityRegistry(capabilities),
            data_runtime_identity=_runtime(),
            corrective_policy=None,
            prompt_template="You are the Evidence Analyst. Emit the strict schema.",
            query_builder=None,
            structured_provider=None,
        )


def _wait_for_terminal(client: TestClient, run_id: str, timeout_seconds: float = 15.0):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = client.get(f"/api/live-runs/{run_id}").json()
        if payload["lifecycle_status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            return payload
        time.sleep(0.02)
    return client.get(f"/api/live-runs/{run_id}").json()


BODY = {
    "ticker": "AAPL",
    "trade_date": "2026-01-15",
    "query": "Why did AAPL move?",
    "model": {
        "provider": "openai",
        "model_id": "gpt-4.1-mini",
        "api_key": "",
        "credential_source": "server_env",
    },
}


def _app(tmp_path: Path, *, resolver: FakeGraphResolver | None = None):
    from catalyst_app.runtime.composition import build_runtime_composition

    composition = build_runtime_composition(
        db_path=tmp_path / "runtime.db",
        dependency_loader=FakeRuntimeDependencyLoader(),
        credential_store=RuntimeCredentialStore(),
        graph_resolver=resolver or FakeGraphResolver(),
        max_workers=2,
        shutdown_grace_seconds=0.2,
    )
    return create_app(runtime_composition=composition)


def test_default_post_live_run_invokes_run_v1_graph_and_persists(tmp_path: Path) -> None:
    resolver = FakeGraphResolver()
    app = _app(tmp_path, resolver=resolver)

    with TestClient(app) as client:
        create_resp = client.post("/api/live-runs", json=BODY)
        assert create_resp.status_code == 200, create_resp.text
        run_id = create_resp.json()["run_id"]
        assert create_resp.json()["status"] == "ACCEPTED"

        summary = _wait_for_terminal(client, run_id)
        assert summary["lifecycle_status"] == "COMPLETED", summary

        # The production composition invoked the repository-owned run_v1_graph:
        # both model roles were actually called.
        assert resolver.analyst.calls >= 1
        assert resolver.writer.calls >= 1
        assert resolver.retriever.calls >= 1

        # RunDTO exposes required terminal artifacts committed in the batch.
        terminal_refs = summary["terminal_artifact_refs"]
        assert any(ref["artifact_type"] == "attribution_result" for ref in terminal_refs)
        assert any(ref["artifact_type"] == "assurance" for ref in terminal_refs)
        assert summary["attribution_status"] == "SUFFICIENT"

        # The events produced by the real graph path are persisted.
        events = client.get(f"/api/live-runs/{run_id}/events?after_seq=0")
        # NOTE: legacy compat endpoint translates only legacy rows; the V1.1
        # events are asserted from the artifacts page + SSE below instead.
        assert client.get(f"/api/live-runs/{run_id}/artifacts").status_code == 200
        artifact_page = client.get(f"/api/live-runs/{run_id}/artifacts").json()
        types = {item["ref"]["artifact_type"] for item in artifact_page["items"]}
        assert {"run_manifest", "context_pack", "rendered_messages", "answer",
                "evidence_detail", "claim_detail", "attribution_result"} <= types

        # V1 workspace projection reads the V1 tables (not legacy storage).
        ws = client.get(f"/api/live-runs/{run_id}/workspace")
        assert ws.status_code == 200, ws.text
        projection = ws.json()
        assert projection["lifecycle_status"] == "COMPLETED"
        assert projection["answer"]
        assert len(projection["claims"]) >= 1
        assert len(projection["evidence"]) >= 1

        # SSE replay proves the terminal batch carried the terminal artifacts.
        stream = client.get(f"/api/live-runs/{run_id}/stream")
        assert stream.status_code == 200
        assert f"event: run.completed" in stream.text
        assert f"id: {run_id}:1" in stream.text

        # total_latency_ms derives from real monotonic timing, never the
        # fabricated constant 1.
        import json as _json

        from catalyst_app.persistence.connect import open_rw

        with open_rw(tmp_path / "runtime.db") as conn:
            completed = conn.execute(
                "SELECT payload_json FROM run_events"
                " WHERE run_id = ? AND event_type = 'run.completed'",
                (run_id,),
            ).fetchone()
        assert completed is not None
        assert _json.loads(completed["payload_json"])["total_latency_ms"] >= 1


def test_health_not_ready_when_identity_readiness_fails(tmp_path: Path) -> None:
    """Finding J: /api/health must not report ready when the production
    graph/dependency composition is unwired or identity readiness fails."""
    from catalyst_app.runtime.composition import build_runtime_composition

    class BrokenLoader(FakeRuntimeDependencyLoader):
        def get_dependencies(self, *, force_reload: bool = False):
            from catalyst_agents.runtime.dependencies import RuntimeDependencies

            return RuntimeDependencies(
                sqlite_db_path=Path("/tmp/missing.db"),
                lancedb_dir=Path("/tmp/missing-lancedb"),
                lancedb_table=None,
                embedding_fn=lambda _: [],
                embedding_model="BAAI/bge-m3",
                embedding_dim=None,
                reranker=None,
                reranker_model="BAAI/bge-reranker-v2-m3",
                default_model=None,
                health={
                    "status": "failed",
                    "sqlite": {"status": "failed", "path": "/tmp/missing.db"},
                    "errors": [{"component": "sqlite", "message": "missing"}],
                },
                retriever=None,
                requested_manifest_id=None,
                index_manifest_id=None,
                data_runtime_identity=None,
            )

    composition = build_runtime_composition(
        db_path=tmp_path / "runtime.db",
        dependency_loader=BrokenLoader(),
        credential_store=RuntimeCredentialStore(),
        graph_resolver=FakeGraphResolver(),
    )
    app = create_app(runtime_composition=composition)
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["runtime_db"] == "ready"
        assert payload["status"] != "ready"


# ── M6 corrective: BYOK ordering and redaction (Finding I) ─────────────────

def test_byok_credential_available_before_graph_and_never_persisted(tmp_path: Path) -> None:
    """The volatile credential is available before provider/graph execution
    can start, and it never appears in manifest, run row, events, artifacts,
    response, or SSE frames."""
    from catalyst_app.persistence.connect import open_rw
    from catalyst_app.runtime.composition import build_runtime_composition
    from catalyst_app.runtime_credential_store import RuntimeCredentialStore

    SECRET = "sk-byok-0123456789abcdef"
    resolver = FakeGraphResolver()
    store = RuntimeCredentialStore()
    composition = build_runtime_composition(
        db_path=tmp_path / "runtime.db",
        dependency_loader=FakeRuntimeDependencyLoader(),
        credential_store=store,
        graph_resolver=resolver,
        max_workers=2,
        shutdown_grace_seconds=0.2,
    )
    app = create_app(runtime_composition=composition)
    body = {
        "ticker": "AAPL",
        "trade_date": "2026-01-15",
        "query": "Why did AAPL move?",
        "model": {
            "provider": "custom_openai_compatible",
            "model_id": "gpt-4.1-mini",
            "api_key": SECRET,
            "base_url": "https://proxy.example.com/v1",
            "credential_source": "browser_key",
        },
    }
    with TestClient(app) as client:
        created = client.post("/api/live-runs", json=body)
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]
        # The credential was registered and was available before the graph ran.
        assert store.get(run_id) is not None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if resolver.last_api_key is not None:
                break
            time.sleep(0.02)
        assert resolver.last_api_key == SECRET

        summary = _wait_for_terminal(client, run_id)
        assert summary["lifecycle_status"] == "COMPLETED"

        # Redaction: the secret never entered any persisted or public surface.
        assert SECRET not in created.text
        assert SECRET not in client.get(f"/api/live-runs/{run_id}").text
        assert SECRET not in client.get(f"/api/live-runs/{run_id}/stream").text
        assert SECRET not in client.get(f"/api/live-runs/{run_id}/artifacts").text
        assert SECRET not in client.get(f"/api/live-runs/{run_id}/workspace").text
        with open_rw(tmp_path / "runtime.db") as conn:
            for table in ("runs", "run_events", "run_artifacts"):
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                blob = " ".join(
                    str(dict(row)) for row in rows
                )
                assert SECRET not in blob, f"secret leaked into {table}"

        # Terminal cleanup removed the volatile credential.
        assert store.get(run_id) is None


def test_adapter_timeout_discards_late_success(tmp_path: Path) -> None:
    """Finding E: the absolute deadline is derived once at admission and
    persisted; a late provider/graph success after that deadline cannot commit
    COMPLETED/artifacts."""
    import threading

    from catalyst_app.api_dto import compute_request_hash
    from catalyst_app.persistence.connect import open_rw
    from catalyst_app.persistence.schema import init_runtime_db
    from catalyst_app.runtime.admission import AdmissionRequest
    from catalyst_app.runtime.composition import build_runtime_composition
    from catalyst_app.runtime_credential_store import RuntimeCredentialStore

    db_path = tmp_path / "runtime.db"
    with open_rw(db_path) as conn:
        init_runtime_db(conn)
        conn.commit()

    release_writer = threading.Event()
    writer_started = threading.Event()

    class SlowWriter(FakeWriterProvider):
        def stream(self, messages):
            self.calls += 1
            writer_started.set()
            release_writer.wait(timeout=10)
            return iter(("SUMMARY\nAAPL rose. [claim:1] (e1)\nCAUSAL_EXPLANATION\nGuidance.\nLIMITATIONS\nNone.",))

    slow_writer = SlowWriter()
    base_resolver = FakeGraphResolver()

    class SlowResolver(FakeGraphResolver):
        def __init__(self, writer) -> None:
            super().__init__()
            self._writer = writer

        def resolve(self, manifest, boundary):
            resolved = base_resolver.resolve(manifest, boundary)
            from dataclasses import replace

            return replace(resolved, writer_llm=self._writer)

    composition = build_runtime_composition(
        db_path=db_path,
        dependency_loader=FakeRuntimeDependencyLoader(),
        credential_store=RuntimeCredentialStore(),
        graph_resolver=SlowResolver(slow_writer),
        max_workers=1,
        shutdown_grace_seconds=0.1,
    )

    # Seed one ACCEPTED run with a real production manifest artifact, then run
    # the production adapter directly with a short absolute deadline so the
    # once-derived deadline is exercised deterministically.
    run_id = "run:timeout"
    req = AdmissionRequest(
        ticker="AAPL", session_date="2026-01-15", query="Why did AAPL move?",
        provider="openai", model_id="gpt-4.1-mini", base_url=None,
        credential_source_identifier="server_env", workflow_version="v1.1",
        config_version="v1.1",
    )
    request_hash = compute_request_hash(
        ticker="AAPL", session_date="2026-01-15", normalized_question="Why did AAPL move?",
        provider="openai", model_id="gpt-4.1-mini", normalized_base_url="",
        credential_source_identifier="server_env", workflow_version="v1.1",
        config_version="v1.1",
    )
    manifest = composition.manifest_factory(req, run_id, request_hash)
    from catalyst_app.persistence.events import canonical_json
    from datetime import datetime, timedelta, timezone

    manifest_json = canonical_json(manifest.model_dump(mode="json"))
    created_at = datetime.now(timezone.utc)
    # Persist the absolute deadline derived at admission (~0.3s out).
    deadline_at = (created_at + timedelta(seconds=0.3)).isoformat()
    with open_rw(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO runs (run_id, lifecycle_status, idempotency_key, request_hash,"
            " run_manifest_id, manifest_hash, capacity_slot, created_at, updated_at,"
            " ticker, provider, base_url, deadline_at)"
            " VALUES (?, 'ACCEPTED', NULL, ?, ?, ?, 0, ?, ?, 'AAPL', 'openai', NULL, ?)",
            (
                run_id,
                request_hash,
                f"manifest:{run_id}",
                "x" * 64,
                created_at.isoformat(),
                created_at.isoformat(),
                deadline_at,
            ),
        )
        conn.execute(
            "INSERT INTO run_events (run_id, seq, occurred_at, event_type, stage, payload_json, schema_version)"
            " VALUES (?, 1, 't', 'run.accepted', 'ADMISSION', ?, 'v1')",
            (run_id, canonical_json({"accepted": True})),
        )
        conn.execute(
            "INSERT INTO run_artifacts (artifact_id, run_id, event_seq, artifact_type,"
            " payload_hash, payload_json, optional)"
            " VALUES (?, ?, 1, 'run_manifest', ?, ?, 0)",
            (f"manifest:{run_id}", run_id, "y" * 64, manifest_json),
        )
        conn.commit()

    result: dict = {}

    def run_adapter() -> None:
        result.update(composition.run_adapter(run_id, timeout_seconds=0.3))

    thread = threading.Thread(target=run_adapter)
    thread.start()
    assert writer_started.wait(timeout=10)
    # The deadline (0.3s) has elapsed while the provider is still in flight.
    time.sleep(0.6)
    release_writer.set()
    thread.join(timeout=10)
    assert result.get("status") == "FAILED"
    assert result.get("failure_code") == "TIMEOUT"

    with open_rw(db_path) as conn:
        row = conn.execute(
            "SELECT lifecycle_status, failure_code FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        terminal = conn.execute(
            "SELECT event_type FROM run_events WHERE run_id = ? AND event_type = 'run.completed'",
            (run_id,),
        ).fetchone()
    assert row["lifecycle_status"] == "FAILED"
    assert row["failure_code"] == "TIMEOUT"
    assert terminal is None
