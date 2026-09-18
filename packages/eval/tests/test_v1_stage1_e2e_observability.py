"""Phase A end-to-end: real M6 composition -> persisted diagnostics -> run_facts.

This is the offline proof that the production path closes the observability gap
without eval-side authority callbacks. It builds the REAL
``build_runtime_composition`` with deterministic fake *external* boundaries
(retriever returning the real V1.1 ``RetrievalResultSet``, fake LLM providers,
fake observation provider) and drives it through the real
``M6AppSseRunnerAdapter`` over two temporary databases:

* a temporary data DB standing in for the immutable Q-001 derivative, and
* a temporary writable runtime DB (``runtime.sqlite3``) for runs/events/
  artifacts.

It then asserts a normal case and a corrective case both produce real terminal
events, a hash-verified ``run_diagnostics`` artifact, real accounting, and
run_facts built only from persisted facts.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path

import pytest

from catalyst_agents.attribution.provider import ContextInputs
from catalyst_agents.retrieval.corrective import (
    BackendCapability,
    BackendHealth,
    CorrectiveCapabilityRegistry,
)
from catalyst_agents.retrieval.task import EvidenceNeed
from catalyst_agents.runtime.provider_budget import (
    ModelPrice,
    ProviderBudgetExceeded,
    ProviderBudgetGuard,
    RoleTokenLimit,
)
from catalyst_agents.runtime.provider_capability import ModelTransportFailure
from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.canonical.temporal import TemporalIdentity
from catalyst_data.retrieval.v1_result import (
    RetrievalHit,
    RetrievalResultSet,
    StageRank,
    StageScore,
)

from catalyst_app.persistence.connect import open_rw
from catalyst_app.runtime_credential_store import RuntimeCredentialStore
from catalyst_eval.v1_1.case import GoldenCase
from catalyst_eval.v1_1.m6_adapter import M6AppSseRunnerAdapter

from tests.v1_1_fixtures import make_stage1_cases

RUN_ID_NORMAL = "run:e2e:normal"
RUN_ID_CORRECTIVE = "run:e2e:corrective"
# The production corpus manifest identity is a lowercase SHA-256 digest; the
# prepared identity the operator records is that ref plus the app-canonical
# hash of the DataRuntimeIdentity.
CORPUS_MANIFEST_ID = "a" * 64
PREPARED_REF = CORPUS_MANIFEST_ID
# The fixture case driven end-to-end (v1f-003); the fakes are aligned to it.
TICKER = "AAPL"
SESSION_DATE = "2025-06-12"
CUTOFF = "2025-06-12T21:00:00Z"


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _temporal() -> TemporalIdentity:
    return TemporalIdentity(
        session_date=SESSION_DATE,
        market_timezone="America/New_York",
        session_open_at=_utc("2025-06-12T14:30:00Z"),
        session_close_at=_utc(CUTOFF),
        information_window_start_at=_utc("2025-06-11T21:00:00Z"),
        cutoff_at=_utc(CUTOFF),
    )


def _runtime() -> DataRuntimeIdentity:
    return DataRuntimeIdentity(
        data_snapshot_id="b" * 64,
        corpus_manifest_id=CORPUS_MANIFEST_ID,
        fts_index_version="build:fts",
        dense_index_version="d" * 64,
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )


def _hit(evidence_id: str, *, lexical: int, dense: int, fusion: int, reranked: int) -> RetrievalHit:
    return RetrievalHit(
        evidence_id=evidence_id,
        canonical_asset_id=f"v1:asset:{evidence_id}",
        content_version_id=f"v1:content:{evidence_id}",
        corpus_document_id=f"doc:{evidence_id}",
        chunk_id=evidence_id,
        excerpt=f"evidence body for {evidence_id}",
        scores=(
            StageScore(stage="lexical", value=-1.0),
            StageScore(stage="dense", value=0.5),
            StageScore(stage="fusion", value=0.02),
            StageScore(stage="reranked", value=float(10 - reranked)),
        ),
        ranks=(
            StageRank(stage="dense", value=dense),
            StageRank(stage="fusion", value=fusion),
            StageRank(stage="lexical", value=lexical),
            StageRank(stage="reranked", value=reranked),
        ),
        source_class="reported_news",
        content_state="FULL_TEXT",
        eligible_at=_utc("2025-06-12T10:00:00Z"),
        ticker_scope=(TICKER,),
        provider="polygon",
        publisher="Example Wire",
        parse_quality="full",
        retrieval_policy_version="qp:v1",
        temporal_identity=_temporal(),
        data_runtime_identity=_runtime(),
        section_key="body",
        chunk_ordinal=1,
        asset_type="NEWS",
        content_hash=hashlib.sha256(evidence_id.encode()).hexdigest(),
        material_capability="MATERIAL_CAPABLE",
        serving_status="body_candidate",
        temporal_precision="publication_time",
        independence_group_id=f"v1:ind:{evidence_id}",
        canonical_url=f"https://example.test/{evidence_id}",
        evidence_role="INDEPENDENT_REPORT",
    )


class FakeDeterministicRetriever:
    """Deterministic external retriever across the real retriever contract.

    It serves the real V1.1 ``RetrievalResultSet`` *and* the pre-conversion
    observation the production ``ProductionHybridRetriever`` provides (which is
    where the candidate order, rank displacement and served arms come from,
    because the V1.1 hit contract itself is lossy for the pre-rerank stages).
    """

    def __init__(self) -> None:
        self.calls = 0
        # Set by the analyst when it routes FOLLOW_UP: the corrective round
        # then serves one additional hit, which is the observed evidence delta.
        self.include_corrective_evidence = False

    def _result_set(self):
        hits = [
            _hit("e1", lexical=1, dense=2, fusion=1, reranked=1),
            _hit("e2", lexical=2, dense=1, fusion=2, reranked=2),
            _hit("e3", lexical=3, dense=3, fusion=3, reranked=3),
        ]
        if self.include_corrective_evidence:
            hits.append(_hit("e4", lexical=4, dense=4, fusion=4, reranked=4))
        return RetrievalResultSet(
            hits=tuple(hits),
            temporal_identity=_temporal(),
            data_runtime_identity=_runtime(),
        )

    def _observation(self, evidence_ids):
        from catalyst_data.retrieval.hybrid import (
            HybridRetrievalObservation,
            ObservedHybridRetrieval,
        )

        ranked = tuple(evidence_ids)
        return ObservedHybridRetrieval(
            observation=HybridRetrievalObservation(
                requested_mode="reranked",
                served_mode="reranked",
                ordered_candidate_evidence_ids=ranked,
                ordered_final_ranked_evidence_ids=ranked,
                rank_changes={"e2": -1, "e3": 1},
                duplicate_drops=None,
                arm_names=("lexical", "dense", "fusion", "reranked"),
                degradation_reasons=(),
            )
        )

    def retrieve(self, query, **kwargs):
        return self._result_set()

    def retrieve_with_observation(self, query, **kwargs):
        from catalyst_data.retrieval.hybrid import ObservedHybridRetrieval

        result_set = self._result_set()
        observed = self._observation(
            tuple(hit.evidence_id for hit in result_set.hits)
        )
        self.calls += 1
        return ObservedHybridRetrieval(
            observation=observed.observation, result_set=result_set
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
            expected_prior_sessions=tuple(
                f"2026-01-{d:02d}" for d in range(1, 21)
            ),
            prior_volumes_by_session={
                f"2026-01-{d:02d}": 1000.0 + d for d in range(1, 21)
            },
            benchmark_ticker="SPY",
            benchmark_return_pct=9.5,
            sector_ticker="XLK",
            sector_return_pct=9.2,
            peer_returns_by_ticker={"MSFT": 9.8, "NVDA": 9.6, "GOOGL": 9.4},
        )


class _StructuredSurface:
    def __init__(self, outer):
        self._outer = outer

    def invoke(self, messages):
        result = self._outer.invoke(messages)
        usage_metadata = getattr(self._outer, "usage_metadata", None)
        if usage_metadata is None:
            return result
        return _StructuredResponse(result, usage_metadata)


class _StructuredResponse:
    def __init__(self, payload, usage_metadata):
        import json

        self.content = json.dumps(payload)
        self.usage_metadata = usage_metadata


class FakeAnalystProvider:
    """Round one: READY (normal) or FOLLOW_UP (corrective). Round two: READY."""

    _CAPABILITY = {
        "supports_structured_output": True,
        "supports_true_streaming": True,
        "declares_token_accounting": True,
        "normalizes_timeout_errors": True,
        "capability_revision": "v1.1-capability-1",
    }

    def __init__(
        self, *, corrective: bool, evidence_ids: tuple[str, ...], on_follow_up=None,
        usage_metadata=None,
    ):
        self.corrective = corrective
        self.evidence_ids = evidence_ids
        self._on_follow_up = on_follow_up
        self.usage_metadata = usage_metadata
        self.calls = 0
        self.capability_metadata = dict(self._CAPABILITY)

    def _causal_hypothesis(self, evidence_id: str) -> dict:
        return {
            "hypothesis_ref": "h1",
            "cause_type": "COMPANY_SPECIFIC_CATALYST",
            "statement": "AAPL rose on record guidance.",
            "supporting_evidence_ids": [evidence_id],
            "magnitude_fit": "STRONG",
            "proposed_role": "PRIMARY",
        }

    def invoke(self, messages):
        self.calls += 1
        evidence_id = self.evidence_ids[0]
        if self.corrective and self.calls == 1:
            if self._on_follow_up is not None:
                self._on_follow_up()
            return {
                "schema_version": "1.0",
                "evidence_decisions": [
                    {
                        "evidence_id": evidence_id,
                        "disposition": "SUPPORT",
                        "supports_hypothesis_refs": ["h1"],
                        "reason_code": "material_support",
                    }
                ],
                "candidate_hypotheses": [self._causal_hypothesis(evidence_id)],
                "proposed_missing_evidence": [
                    {
                        "proposal_ref": "p1",
                        "evidence_need": "SECTOR_NEWS",
                        "time_scope": "SESSION_INFORMATION_WINDOW",
                        "expected_information": "Peer/sector confirmation.",
                        "reason_code": "MISSING_SECTOR_CONTEXT",
                    }
                ],
                "research_decision": "FOLLOW_UP",
                "recommended_status": "PARTIAL",
                "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
                "proposed_corrective_intents": [
                    {"proposal_ref": "p1", "query_hints": ["sector peers"]}
                ],
            }
        return {
            "schema_version": "1.0",
            "evidence_decisions": [
                {
                    "evidence_id": evidence_id,
                    "disposition": "SUPPORT",
                    "supports_hypothesis_refs": ["h1"],
                    "reason_code": "material_support",
                }
            ],
            "candidate_hypotheses": [self._causal_hypothesis(evidence_id)],
            "research_decision": "READY",
            "recommended_status": "SUFFICIENT",
            "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
        }

    def with_structured_output(self, schema):
        return _StructuredSurface(self)


class FakeWriterProvider:
    _CAPABILITY = {
        "supports_structured_output": True,
        "supports_true_streaming": True,
        "declares_token_accounting": True,
        "normalizes_timeout_errors": True,
        "capability_revision": "v1.1-capability-1",
    }

    def __init__(self, *, usage_metadata=None) -> None:
        self.calls = 0
        self.usage_metadata = usage_metadata
        self.capability_metadata = dict(self._CAPABILITY)

    def stream(self, messages):
        import re

        self.calls += 1
        prompt = (
            messages[0]["content"]
            if isinstance(messages[0], dict)
            else messages[0].content
        )
        match = re.search(r"claim_id: ([A-Za-z0-9:_-]+)", prompt)
        claim_id = match.group(1) if match else "claim:1"
        text = (
            "SUMMARY\nAAPL rose on record guidance. "
            f"[{claim_id}] (e1)\n"
            "CAUSAL_EXPLANATION\nGuidance raised forward revenue.\n"
            "LIMITATIONS\nNone."
        )
        if self.usage_metadata is None:
            yield text
        else:
            yield {"content": text, "usage_metadata": self.usage_metadata}


class FakeRuntimeDependencyLoader:
    def __init__(self, *, runtime: DataRuntimeIdentity) -> None:
        self._runtime = runtime

    def get_dependencies(self, *, force_reload: bool = False):
        from catalyst_agents.runtime.dependencies import RuntimeDependencies

        return RuntimeDependencies(
            sqlite_db_path=Path("/tmp/e2e-data.db"),
            lancedb_dir=Path("/tmp/lancedb"),
            lancedb_table=None,
            embedding_fn=lambda _: [],
            embedding_model="BAAI/bge-m3",
            embedding_dim=1024,
            reranker=None,
            reranker_model="BAAI/bge-reranker-v2-m3",
            default_model="deepseek-chat",
            health={
                "status": "ready",
                "sqlite": {"status": "ready", "path": "/tmp/e2e-data.db"},
                "lancedb": {"status": "ready", "path": "/tmp/lancedb"},
                "embedding": {"status": "ready", "model": "BAAI/bge-m3"},
                "reranker": {"status": "ready", "model": "BAAI/bge-reranker-v2-m3"},
                "default_model": {"status": "ready", "model": "deepseek-chat"},
                "retrieval": {"status": "ready"},
                "errors": [],
            },
            retriever=None,
            requested_manifest_id=CORPUS_MANIFEST_ID,
            index_manifest_id="d" * 64,
            data_runtime_identity=self._runtime,
        )


class DeterministicGraphResolver:
    """External-boundary resolver returning deterministic providers."""

    def __init__(
        self, *, corrective: bool, evidence_ids: tuple[str, ...],
        analyst_usage_metadata=None, writer_usage_metadata=None,
    ) -> None:
        from catalyst_agents.runtime.retrieval_adapter import AgentRetrieverAdapter

        self.raw_retriever = FakeDeterministicRetriever()
        self.analyst = FakeAnalystProvider(
            corrective=corrective,
            evidence_ids=evidence_ids,
            on_follow_up=lambda: setattr(
                self.raw_retriever, "include_corrective_evidence", True
            ),
            usage_metadata=analyst_usage_metadata,
        )
        self.writer = FakeWriterProvider(usage_metadata=writer_usage_metadata)
        # The production retriever boundary is the agents' retrieval adapter,
        # which emits the observations the diagnostics are built from.
        self.retriever = AgentRetrieverAdapter(self.raw_retriever)

    def resolve(self, manifest, boundary):
        capabilities = {}
        for name in (
            "COMPANY_PRIMARY",
            "COMPANY_NEWS",
            "SECTOR_NEWS",
            "MACRO_EVENT",
            "MACRO_SERIES",
            "FUNDAMENTALS",
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


def _guard(max_calls: int = 12, max_cost: float = 1.0) -> ProviderBudgetGuard:
    return ProviderBudgetGuard(
        max_provider_calls=max_calls,
        max_cost_usd=max_cost,
        prices={
            ("deepseek", "deepseek-chat"): ModelPrice(
                provider="deepseek",
                model_id="deepseek-chat",
                input_usd_per_million_tokens=1.0,
                output_usd_per_million_tokens=2.0,
            )
        },
        role_token_limits={
            "evidence_analyst": RoleTokenLimit(2000, 800),
            "streaming_writer": RoleTokenLimit(2000, 800),
        },
    )


def _composition(
    tmp_path: Path, *, corrective: bool, evidence_ids, guard,
    analyst_usage_metadata=None, writer_usage_metadata=None,
):
    """Real composition, real ProductionRunAdapter, real run_v1_graph."""
    from catalyst_app.runtime.composition import build_runtime_composition

    runtime_db = tmp_path / "runtime.sqlite3"
    store = RuntimeCredentialStore()
    composition = build_runtime_composition(
        db_path=runtime_db,
        dependency_loader=FakeRuntimeDependencyLoader(runtime=_runtime()),
        credential_store=store,
        graph_resolver=DeterministicGraphResolver(
            corrective=corrective,
            evidence_ids=evidence_ids,
            analyst_usage_metadata=analyst_usage_metadata,
            writer_usage_metadata=writer_usage_metadata,
        ),
        provider_budget=guard,
        max_workers=2,
        shutdown_grace_seconds=0.2,
    )
    return composition, runtime_db, store


def test_real_composition_whole_run_tokens_require_every_role_usage(tmp_path):
    """Writer usage alone must never be published as whole-run provider usage."""
    guard = _guard()
    composition, runtime_db, _store = _composition(
        tmp_path,
        corrective=False,
        evidence_ids=("e1",),
        guard=guard,
        analyst_usage_metadata=None,
        writer_usage_metadata={"input_tokens": 50, "output_tokens": 10},
    )
    guard.begin_case(provider_calls=6, cost_usd=1.0)
    adapter = _adapter(runtime_db, composition=composition)
    adapter.set_remaining_budget(provider_calls=6, cost_usd=1.0)

    outcome = adapter.run_case(_case())

    assert outcome.terminal_status == "COMPLETED"
    assert outcome.tokens is None
    assert outcome.run_facts["tokens"] is None
    with open_rw(runtime_db) as conn:
        import json

        run_id = outcome.run_manifest_id.split(":", 1)[1]
        row = conn.execute(
            "SELECT payload_json FROM run_artifacts WHERE run_id = ? "
            "AND artifact_type = 'run_diagnostics'",
            (run_id,),
        ).fetchone()
    provider = json.loads(row["payload_json"])["provider"]
    assert provider["total_tokens_in"] is None
    assert provider["total_tokens_out"] is None
    assert outcome.cost_usd is not None and outcome.cost_usd > 0


def _case() -> GoldenCase:
    """The fixture case the deterministic fakes are aligned to (v1f-003)."""
    for row in make_stage1_cases():
        if row["case_id"] == "v1f-003":
            assert row["ticker"] == TICKER and row["session_date"] == SESSION_DATE
            return GoldenCase.model_validate(row)
    raise AssertionError("fixture case v1f-003 not found")


def _data_db(tmp_path: Path) -> Path:
    """A separate read-only stand-in for the immutable Q-001 derivative."""
    db = tmp_path / "q001-data.db"
    db.write_bytes(b"immutable-q001-derivative\x00" * 32)
    return db


def _adapter(db_path: Path, *, composition) -> M6AppSseRunnerAdapter:
    """The real adapter over the real composition.

    ``credential_provider`` is the adapter's own pre-submit credential path;
    there is no eval-side authority callback and no external run input.
    """
    return M6AppSseRunnerAdapter(
        composition=composition,
        db_path=db_path,
        prepared_identity_ref=PREPARED_REF,
        prepared_identity_hash=_identity_hash(),
        case_timeout_seconds=20.0,
        credential_provider=lambda: "test-key",
    )


def _identity_hash() -> str:
    """The identity hash the production manifest factory records (app-canonical)."""
    from catalyst_app.persistence.events import payload_sha256

    return payload_sha256(_runtime().model_dump(mode="json"))


def test_e2e_offline_normal_case_persists_and_seals_facts(tmp_path):
    """Seal one real case end-to-end through M6AppSseRunnerAdapter.

    Everything comes from the real composition + persisted rows: no eval-side
    authority callback, no hand-seeded diagnostics, no synthetic facts.
    """
    guard = _guard()
    composition, runtime_db, _store = _composition(
        tmp_path, corrective=False, evidence_ids=("e1",), guard=guard
    )
    data_db = _data_db(tmp_path)
    data_before = data_db.read_bytes()
    case = _case()

    adapter = _adapter(runtime_db, composition=composition)
    guard.begin_case(provider_calls=6, cost_usd=1.0)
    adapter.set_remaining_budget(provider_calls=6, cost_usd=1.0)
    outcome = adapter.run_case(case)

    run_id = outcome.run_manifest_id.split(":", 1)[1]

    assert outcome.case_id == case.case_id
    assert outcome.terminal_status == "COMPLETED"
    facts = outcome.run_facts
    assert facts["schema_version"] == "v1_1_stage1_run_facts_v1"
    assert facts["run_id"] == run_id
    assert facts["latency_ms"] == outcome.latency_ms
    assert facts["tokens"] == outcome.tokens
    assert facts["cost_usd"] == outcome.cost_usd
    assert facts["output_status"] in {"SUFFICIENT", "PARTIAL", "ABSTAIN"}
    # Retrieval facts sealed from the persisted diagnostics.
    assert facts["retrieval"]["observed"] is True
    assert facts["retrieval"]["ranked_evidence_ids"] == ["e1", "e2", "e3"]
    # Candidate order comes from the retriever's own pre-conversion
    # observation, which the lossy V1.1 hit contract cannot expose.
    assert facts["retrieval"]["candidate_evidence_ids"] == ["e1", "e2", "e3"]
    assert facts["retrieval"]["pool"]["chunk_inventory"] == ["e1", "e2", "e3"]
    assert facts["retrieval"]["reranker_contributed"] is True
    assert facts["retrieval"]["ticker_violations"] == []
    assert facts["retrieval"]["cutoff_violations"] == []
    assert facts["retrieval"]["latency_ms"] is not None
    # Trajectory facts are observed, never defaulted judgements.
    assert facts["trajectory"]["corrective_triggered"] is False
    assert "stop_correct" not in facts["trajectory"]
    assert "corrected" not in facts["trajectory"]
    # Accounting is real and reconciled against the shared guard.
    assert facts["provider_accounting"]["provider_calls"] == 2
    # The fixture provider deliberately omits usage metadata: token accounting
    # remains unknown rather than becoming a zero-token/$0 observation.
    assert facts["provider_accounting"]["tokens_in"] is None
    assert facts["provider_accounting"]["tokens_out"] is None
    assert facts["tokens"] is None
    guard.reconcile_case(
        provider_calls=facts["provider_accounting"]["provider_calls"],
        cost_usd=facts["provider_accounting"]["cost_usd"],
    )

    import json

    with open_rw(runtime_db) as conn:
        rows = conn.execute(
            "SELECT artifact_type, payload_json FROM run_artifacts WHERE run_id = ?"
            " ORDER BY event_seq ASC, artifact_id ASC",
            (run_id,),
        ).fetchall()
    artifact_types = {str(r["artifact_type"]) for r in rows}
    assert "run_diagnostics" in artifact_types
    assert "run_manifest" in artifact_types
    assert "attribution_result" in artifact_types
    assert "assurance" in artifact_types
    assert "context_pack" in artifact_types

    diagnostics_rows = [
        r for r in rows if str(r["artifact_type"]) == "run_diagnostics"
    ]
    attribution_status = json.loads(
        [
            r for r in rows if str(r["artifact_type"]) == "attribution_result"
        ][0]["payload_json"]
    )["attribution_status"]

    diagnostics = json.loads(diagnostics_rows[0]["payload_json"])
    assert diagnostics["schema_version"] == "v1.1_run_diagnostics_v1"
    assert diagnostics["run_id"] == run_id
    assert diagnostics["retrieval"]["observed"] is True
    # Rank order observed from the served retrieval result set.
    assert diagnostics["retrieval"]["ordered_final_ranked_evidence_ids"] == [
        "e1",
        "e2",
        "e3",
    ]
    assert diagnostics["retrieval"]["ticker_violations"] == []
    assert diagnostics["retrieval"]["cutoff_violations"] == []
    assert diagnostics["retrieval"]["measured_latency_ms"] is not None
    # Provider accounting is real: the analyst and writer were both invoked.
    assert diagnostics["provider"]["analyst_provider_attempts"] == 1
    assert diagnostics["provider"]["writer_provider_attempts"] == 1
    assert diagnostics["provider"]["cost_method"] in {
        "reported",
        "upper_bound_charged",
    }
    assert diagnostics["trajectory"]["corrective_rounds_executed"] == 0
    assert diagnostics["terminal"]["result_status"] in {
        "SUFFICIENT",
        "PARTIAL",
        "ABSTAIN",
    }
    assert diagnostics["terminal"]["result_status"] == attribution_status
    # No runtime self-judgement is persisted.
    assert "stop_correct" not in diagnostics["trajectory"]

    # The immutable data DB was never written by the runtime.
    assert data_db.read_bytes() == data_before
    for suffix in ("-wal", "-shm", "-journal"):
        assert not data_db.with_name(data_db.name + suffix).exists()


def test_e2e_offline_corrective_case_records_observed_round(tmp_path):
    """A real corrective round is observed, persisted, and sealed as facts."""
    guard = _guard()
    composition, runtime_db, _store = _composition(
        tmp_path, corrective=True, evidence_ids=("e1",), guard=guard
    )
    case = _case()
    adapter = _adapter(runtime_db, composition=composition)
    guard.begin_case(provider_calls=12, cost_usd=1.0)
    adapter.set_remaining_budget(provider_calls=12, cost_usd=1.0)
    outcome = adapter.run_case(case)
    run_id = outcome.run_manifest_id.split(":", 1)[1]
    assert outcome.terminal_status == "COMPLETED"

    facts = outcome.run_facts
    assert facts["trajectory"]["corrective_triggered"] is True
    assert facts["trajectory"]["rounds_executed"] == 1
    assert facts["trajectory"]["gap_reason_codes"] == ["MISSING_SECTOR_CONTEXT"]
    assert facts["trajectory"]["corrective_actions"]
    assert facts["trajectory"]["research_fingerprints"]
    assert facts["trajectory"]["evidence_delta_ids"] == ["e4"]

    import json

    with open_rw(runtime_db) as conn:
        row = conn.execute(
            "SELECT payload_json FROM run_artifacts WHERE run_id = ?"
            " AND artifact_type = 'run_diagnostics'",
            (run_id,),
        ).fetchone()
    diagnostics = json.loads(row["payload_json"])
    trajectory = diagnostics["trajectory"]
    assert trajectory["corrective_rounds_executed"] == 1
    round_record = trajectory["rounds"][0]
    assert round_record["gap_reason_codes"] == ["MISSING_SECTOR_CONTEXT"]
    assert round_record["evidence_needs"] == ["SECTOR_NEWS"]
    assert round_record["action_ids"]
    assert round_record["research_fingerprints"]
    assert round_record["evidence_delta"]["added_evidence_ids"] == ["e4"]
    assert round_record["evidence_delta"]["removed_evidence_ids"] == []
    # The corrective round is the second analyst logical call.
    assert diagnostics["provider"]["analyst_provider_attempts"] >= 2


def test_e2e_offline_budget_exhaustion_stops_before_inner_provider_attempt(
    tmp_path,
):
    """A budget too small for the remaining provider work fails closed at the
    provider boundary, before the inner attempt is dispatched."""
    guard = _guard(max_calls=1, max_cost=1.0)
    composition, runtime_db, _store = _composition(
        tmp_path, corrective=True, evidence_ids=("e1",), guard=guard
    )
    from catalyst_app.runtime.admission import AdmissionRequest

    guard.begin_case(provider_calls=1, cost_usd=1.0)
    admitted = composition.admission.admit(
        AdmissionRequest(
            ticker=TICKER,
            session_date=SESSION_DATE,
            query="Why did AAPL move?",
            provider="deepseek",
            model_id="deepseek-chat",
            base_url=None,
            credential_source_identifier="server_env",
            workflow_version="v1.1",
            config_version="v1.1",
        )
    )
    assert admitted.kind == "accepted"
    run_id = admitted.run_id

    import time

    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if composition.claimer.current_lifecycle(run_id).value in {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
        }:
            break
        time.sleep(0.05)
    lifecycle = composition.claimer.current_lifecycle(run_id)
    assert lifecycle is not None and lifecycle.value == "FAILED"
    # Exactly one provider call was spent; the guard refused the next one.
    assert guard.used_provider_calls == 1


def test_provider_budget_guard_refuses_an_uncosted_model(tmp_path):
    """No identity-bound price -> the guard fails closed rather than guessing."""
    from catalyst_agents.runtime.provider_budget import ProviderPriceUnavailable

    guard = ProviderBudgetGuard(
        max_provider_calls=4,
        max_cost_usd=1.0,
        prices={},
        role_token_limits={"evidence_analyst": RoleTokenLimit(10, 10)},
    )
    guard.begin_case(provider_calls=4, cost_usd=1.0)
    with pytest.raises(ProviderPriceUnavailable):
        guard.reserve(
            role="evidence_analyst", provider="deepseek", model_id="deepseek-chat"
        )


def test_provider_budget_guard_refuses_before_exceeding_the_case_budget():
    guard = _guard(max_calls=10, max_cost=1.0)
    guard.begin_case(provider_calls=1, cost_usd=1.0)
    guard.reserve(role="evidence_analyst", provider="deepseek", model_id="deepseek-chat")
    with pytest.raises(ProviderBudgetExceeded):
        guard.reserve(
            role="evidence_analyst", provider="deepseek", model_id="deepseek-chat"
        )


# ---------------------------------------------------------------------------
# supervisor blocker B: a FAILED run keeps the provider work it consumed
# ---------------------------------------------------------------------------

class NonRetryableProviderError(Exception):
    """A provider failure outside the bounded-retry taxonomy."""


class FailingAnalystProvider:
    """Reached and dispatched, then fails non-retryably on every attempt."""

    _CAPABILITY = FakeAnalystProvider._CAPABILITY

    def __init__(self, *, retryable: bool = False) -> None:
        self.calls = 0
        self.retryable = retryable
        self.capability_metadata = dict(self._CAPABILITY)

    def invoke(self, messages):
        self.calls += 1
        if self.retryable:
            raise ModelTransportFailure("provider unavailable after dispatch")
        raise NonRetryableProviderError("provider rejected the request after dispatch")

    def with_structured_output(self, schema):
        return _StructuredSurface(self)


class FailingAnalystGraphResolver(DeterministicGraphResolver):
    """Same deterministic boundary, but the analyst always fails."""

    def __init__(self, *, retryable: bool = False) -> None:
        super().__init__(corrective=False, evidence_ids=("e1",))
        from catalyst_agents.runtime.retrieval_adapter import AgentRetrieverAdapter

        self.analyst = FailingAnalystProvider(retryable=retryable)
        self.retriever = AgentRetrieverAdapter(self.raw_retriever)


def test_e2e_offline_failed_run_persists_consumed_provider_accounting(tmp_path):
    """A real FAILED run keeps the dispatched provider call and its cost.

    The analyst provider is dispatched once and then fails non-retryably. The
    run terminalizes FAILED, and the durable provider-accounting artifact must
    record the real consumed call/cost so the eval ledger can deduct it instead
    of reporting a zero-cost failure.
    """
    import json

    guard = _guard()
    runtime_db = tmp_path / "runtime.sqlite3"
    store = RuntimeCredentialStore()
    from catalyst_app.runtime.composition import build_runtime_composition

    composition = build_runtime_composition(
        db_path=runtime_db,
        dependency_loader=FakeRuntimeDependencyLoader(runtime=_runtime()),
        credential_store=store,
        graph_resolver=FailingAnalystGraphResolver(),
        provider_budget=guard,
        max_workers=2,
        shutdown_grace_seconds=0.2,
    )
    case = _case()
    adapter = _adapter(runtime_db, composition=composition)
    guard.begin_case(provider_calls=6, cost_usd=1.0)
    adapter.set_remaining_budget(provider_calls=6, cost_usd=1.0)

    outcome = adapter.run_case(case)
    run_id = outcome.run_manifest_id.split(":", 1)[1]

    assert outcome.terminal_status == "FAILED"
    # The real consumed accounting survived the FAILED terminal status.
    assert guard.used_provider_calls == 1
    assert outcome.provider_calls == 1
    assert outcome.cost_usd == pytest.approx(guard.used_cost_usd)
    assert outcome.run_facts["provider_accounting"]["provider_calls"] == 1
    assert outcome.run_facts["terminal_status"] == "FAILED"
    assert outcome.run_facts["latency_ms"] is None
    assert outcome.run_facts["tokens"] is None
    assert outcome.run_facts["cost_usd"] == outcome.cost_usd

    with open_rw(runtime_db) as conn:
        row = conn.execute(
            "SELECT payload_json FROM run_artifacts WHERE run_id = ?"
            " AND artifact_type = 'run_provider_accounting'",
            (run_id,),
        ).fetchone()
    assert row is not None, "the FAILED run must persist its provider accounting"
    accounting = json.loads(row["payload_json"])
    assert accounting["schema_version"] == "v1.1_run_provider_accounting_v1"
    assert accounting["run_id"] == run_id
    assert accounting["provider"]["analyst_logical_calls"] == 1
    assert accounting["provider"]["analyst_provider_attempts"] == 1
    assert accounting["provider"]["writer_logical_calls"] == 0
    assert accounting["provider"]["writer_provider_attempts"] == 0
    assert accounting["provider_calls"] == 1
    assert accounting["provider"]["cost_usd"] == pytest.approx(guard.used_cost_usd)
    assert accounting["outstanding_cost_usd"] == pytest.approx(0.0)


def test_e2e_failed_retry_persists_one_logical_call_and_two_attempts(tmp_path):
    """A retry is two real attempts but one Analyst logical call."""
    import json

    guard = _guard()
    runtime_db = tmp_path / "runtime.sqlite3"
    store = RuntimeCredentialStore()
    from catalyst_app.runtime.composition import build_runtime_composition

    composition = build_runtime_composition(
        db_path=runtime_db,
        dependency_loader=FakeRuntimeDependencyLoader(runtime=_runtime()),
        credential_store=store,
        graph_resolver=FailingAnalystGraphResolver(retryable=True),
        provider_budget=guard,
        max_workers=2,
        shutdown_grace_seconds=0.2,
    )
    adapter = _adapter(runtime_db, composition=composition)
    guard.begin_case(provider_calls=6, cost_usd=1.0)
    adapter.set_remaining_budget(provider_calls=6, cost_usd=1.0)

    outcome = adapter.run_case(_case())
    run_id = outcome.run_manifest_id.split(":", 1)[1]

    assert outcome.terminal_status == "FAILED"
    assert outcome.provider_calls == 2
    with open_rw(runtime_db) as conn:
        row = conn.execute(
            "SELECT payload_json FROM run_artifacts WHERE run_id = ?"
            " AND artifact_type = 'run_provider_accounting'",
            (run_id,),
        ).fetchone()
    assert row is not None
    accounting = json.loads(row["payload_json"])["provider"]
    assert accounting["analyst_logical_calls"] == 1
    assert accounting["analyst_provider_attempts"] == 2
    assert accounting["writer_logical_calls"] == 0
    assert accounting["writer_provider_attempts"] == 0
