"""Wave 3 user-smoke runner contracts (C2/C3).

The user-smoke runner is a full user-visible attribution smoke through the
real Catalyst HTTP surface (loopback) into the live-run service, attribution
graph, retrieval, provider, and exported trace/assurance evidence. These tests
prove behavior, not snapshots:

- stable case selection (g006/g007/h004) with h004 query_override preserved;
- Wave 2 evidence validation (T4 + full four-arm `FOUR_ARM_E2E_OK`) that
  rejects forgeable/absent/partial gates;
- real loopback HTTP composition at the highest feasible local boundary with
  the provider mocked only at the graph/LLM adapter seam;
- production gates run before any provider call or artifact write;
- API keys are never accepted by the CLI and never appear in evidence;
- bounded poll/retry/timeout behavior;
- failure leaves no final artifact directory/token;
- partial/mocked/degraded/expectation-mismatch/unknown-cost runs cannot write
  the success token;
- a valid all-pass run writes exact `USER_SMOKE_OK` and checksum-covered
  evidence;
- the protected frozen DB is only ever opened read-only by the runner.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from catalyst_agents.runtime.service import LiveRunService
from catalyst_agents.trace.artifacts import write_node_artifact
from catalyst_agents.trace.schema import init_trace_db
from catalyst_agents.trace.writer import TraceWriter
from catalyst_app.main import create_app
from catalyst_app.runtime_credential_store import RuntimeCredentialStore
from catalyst_app.workbench_store import WorkbenchStore

from catalyst_eval.post_import.case_pack import (
    CasePackCase,
    SCHEMA_VERSION as CASE_PACK_SCHEMA_VERSION,
    build_smoke_case_pack,
    compute_case_pack_id,
    write_case_pack,
)
from catalyst_eval.post_import.four_arm import EmbeddingBoundary
from catalyst_eval.post_import.index_identity import ResolvedRuntimeIdentity
from catalyst_eval.post_import.probe import (
    CaseProbeResult,
    ServedCorpusProbeReport,
    write_probe_evidence,
)
from catalyst_eval.post_import.t4_evidence import ValidatedT4Evidence
from catalyst_eval.post_import.user_smoke import (
    COST_CEILING_USD,
    USER_SMOKE_CASE_IDS,
    USER_SMOKE_EXPECTED_CLASSES,
    USER_SMOKE_TOKEN,
    WAVE2_FOUR_ARM_TOKEN,
    WAVE_TOKEN_FILENAME,
    HttpResponse,
    UserSmokeSummary,
    _redact_payload,
    _secret_scan,
    run_user_smoke,
    select_user_smoke_cases,
    validate_wave2_evidence,
)

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden_set"


@pytest.fixture(autouse=True)
def _fake_deepseek_env(monkeypatch):
    """The app's server_env credential gate requires a configured key.

    The provider itself is mocked at the graph/LLM adapter seam in these
    tests, so the value is a synthetic placeholder that never reaches the
    network and never appears in evidence.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake-test-only-00000000000000000000")

GIT_HEAD = "8dd9ee9b5f04e848e3d8248dad6470189af79573"
CODE_REVISION = "bb43ebe20f29a13ef426e0a1a7c3aefc6d15ffd8"

EVIDENCE_KWARGS = {
    "db_sha256": "bb37b213091e256033fa00272cb7a85617dbcddf69d6fe9b515840cd9f1ebe40",
    "corpus_manifest_id": "3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc",
    "snapshot_id": "7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49",
    "source_bundle_id": "8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2",
    "probe_report_id": "25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23",
    "postbuild_readiness_id": "9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b",
    "index_manifest_id": "c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083",
    "db_path": "data/snapshots/catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db",
    "db_user_version": 13,
    "db_foreign_key_violations": 0,
    "lancedb_dir": "data/lancedb_gold/b6g_8ffae891b4e1",
    "active_table_name": "chunks__staging__b3761f4b943542a8",
    "model_name": "BAAI/bge-m3",
    "model_revision": "5617a9f61b028005a4858fdac845db406aefb181",
    "tokenizer_revision": "5617a9f61b028005a4858fdac845db406aefb181",
    "dimension": 1024,
    "dtype": "float32",
    "normalization_mode": "l2",
    "embedding_mode": "mock_unit_test",
}


def _resolved(**overrides) -> ResolvedRuntimeIdentity:
    values = {
        "lancedb_dir": Path("data/lancedb_gold/b6g_8ffae891b4e1"),
        "active_table_name": "chunks__staging__b3761f4b943542a8",
        "snapshot_id": EVIDENCE_KWARGS["snapshot_id"],
        "corpus_manifest_id": EVIDENCE_KWARGS["corpus_manifest_id"],
        "source_bundle_id": EVIDENCE_KWARGS["source_bundle_id"],
        "index_manifest_id": EVIDENCE_KWARGS["index_manifest_id"],
        "probe_report_id": EVIDENCE_KWARGS["probe_report_id"],
        "postbuild_readiness_id": EVIDENCE_KWARGS["postbuild_readiness_id"],
        "code_revision": CODE_REVISION,
        "git_head": GIT_HEAD,
        "model_name": "BAAI/bge-m3",
        "model_revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "tokenizer_revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "dimension": 1024,
        "dtype": "float32",
        "normalization_mode": "l2",
        "vector_count": 295506,
        "db_path": Path(EVIDENCE_KWARGS["db_path"]),
        "db_sha256": EVIDENCE_KWARGS["db_sha256"],
        "db_user_version": 13,
        "db_foreign_key_violations": 0,
        "lancedb_row_count": 295506,
    }
    values.update(overrides)
    return ResolvedRuntimeIdentity(**values)


def _build_t4_evidence(
    tmp_path: Path,
    *,
    cases=None,
    resolved=None,
) -> Path:
    cases = cases if cases is not None else build_smoke_case_pack(GOLDEN_DIR)
    resolved = resolved if resolved is not None else _resolved()
    case_pack_id = compute_case_pack_id(cases)
    report = ServedCorpusProbeReport(
        schema_version="served_corpus_probe_v1",
        corpus_manifest_id=EVIDENCE_KWARGS["corpus_manifest_id"],
        case_count=len(cases),
        passed_count=len(cases),
        all_passed=True,
        per_case=tuple(
            CaseProbeResult(c.case_id, c.ticker, c.cutoff, 1) for c in cases
        ),
    )
    evidence_dir = tmp_path / "t4_evidence"
    write_case_pack(cases, evidence_dir / "case_pack.jsonl")
    evidence_kwargs = {
        k: v for k, v in EVIDENCE_KWARGS.items()
        if k not in {"case_pack_id", "case_pack_path"}
    }
    evidence_kwargs["db_path"] = str(resolved.db_path)
    evidence_kwargs["lancedb_dir"] = str(resolved.lancedb_dir)
    write_probe_evidence(
        report,
        run_dir=evidence_dir,
        case_pack_id=case_pack_id,
        case_pack_path="case_pack.jsonl",
        runtime_git_head=GIT_HEAD,
        index_build_code_revision=CODE_REVISION,
        **evidence_kwargs,
    )
    return evidence_dir


def _build_wave2_evidence(
    tmp_path: Path,
    *,
    resolved=None,
    token: str = WAVE2_FOUR_ARM_TOKEN,
    case_count: int = 10,
    embedding_mode: str = "production_pinned",
    arm_count: int = 10,
    pool_count: int = 10,
    meta_extra: dict | None = None,
    cases=None,
    valid_artifacts: bool = True,
) -> Path:
    """Build Wave 2 evidence. When valid_artifacts is True, write reloadable
    arm/pool artifacts under the AMEND-5.1 schema (required for validate_wave2).
    """
    from catalyst_data.config import BGE_M3_REVISION, BGE_RERANKER_REVISION
    from catalyst_data.retrieval.artifacts import write_arm_artifact
    from catalyst_data.retrieval.pool import generate_union_pool, write_union_pool

    resolved = resolved if resolved is not None else _resolved()
    wave2 = tmp_path / f"wave2_full_{uuid.uuid4().hex[:8]}"
    (wave2 / "arms").mkdir(parents=True)
    (wave2 / "pool").mkdir(parents=True)
    if cases is None:
        cases = _full_cases() if case_count == 10 else [
            CasePackCase(
                schema_version=CASE_PACK_SCHEMA_VERSION,
                case_id=f"case-{idx}", ticker="AAPL", session_date="2026-01-15",
                cutoff="2026-01-15T21:00:00Z", query="q", source_set="fixture",
                golden={"golden_id": f"case-{idx}", "should_refuse": False},
            )
            for idx in range(case_count)
        ]
    case_ids = [case.case_id for case in cases][:arm_count]

    def _arm_result(chunk_ids, *, lexical=False, dense=False, rerank=False):
        results = []
        for position, chunk_id in enumerate(chunk_ids, start=1):
            results.append({
                "chunk_id": chunk_id,
                "document_id": f"doc:{chunk_id}",
                "available_at": "2026-01-01T00:00:00Z",
                "source_class": "reported_news",
                "rank": position,
                "lexical_raw_score": 0.1 if lexical else None,
                "lexical_rank": position if lexical else None,
                "dense_score": 0.1 if dense else None,
                "dense_rank": position if dense else None,
                "fusion_score": 0.1,
                "fusion_rank": position,
                "arm_ranks": [],
                "arm_scores": [],
                "reranker_score": float(10 - position) if rerank else None,
                "reranker_rank": position if rerank else None,
            })
        return results

    wave2_run_id = "wave2"
    cases_by_id = {case.case_id: case for case in cases}
    if valid_artifacts:
        for case_id in case_ids:
            case = cases_by_id[case_id]
            arms = {
                "fts5": {
                    "mode_requested": "fts5", "mode_served": "fts5", "status": "ok",
                    "latency_ms": 1.0, "degradation_reasons": [],
                    "results": _arm_result(("a", "b", "c"), lexical=True),
                },
                "dense": {
                    "mode_requested": "dense", "mode_served": "dense", "status": "ok",
                    "latency_ms": 1.0, "degradation_reasons": [],
                    "results": _arm_result(("b", "d"), dense=True),
                },
                "hybrid": {
                    "mode_requested": "hybrid", "mode_served": "hybrid", "status": "ok",
                    "latency_ms": 1.0, "degradation_reasons": [],
                    "results": [
                        {
                            "chunk_id": "d", "document_id": "doc:d",
                            "available_at": "2026-01-01T00:00:00Z",
                            "source_class": "reported_news", "rank": 1,
                            "lexical_raw_score": None, "lexical_rank": None,
                            "dense_score": 0.2, "dense_rank": 1,
                            "fusion_score": 0.3, "fusion_rank": 1,
                            "arm_ranks": [], "arm_scores": [],
                            "reranker_score": None, "reranker_rank": None,
                        },
                        {
                            "chunk_id": "a", "document_id": "doc:a",
                            "available_at": "2026-01-01T00:00:00Z",
                            "source_class": "reported_news", "rank": 2,
                            "lexical_raw_score": 0.1, "lexical_rank": 1,
                            "dense_score": None, "dense_rank": None,
                            "fusion_score": 0.2, "fusion_rank": 2,
                            "arm_ranks": [], "arm_scores": [],
                            "reranker_score": None, "reranker_rank": None,
                        },
                        {
                            "chunk_id": "e", "document_id": "doc:e",
                            "available_at": "2026-01-01T00:00:00Z",
                            "source_class": "reported_news", "rank": 3,
                            "lexical_raw_score": 0.05, "lexical_rank": 2,
                            "dense_score": 0.05, "dense_rank": 2,
                            "fusion_score": 0.1, "fusion_rank": 3,
                            "arm_ranks": [], "arm_scores": [],
                            "reranker_score": None, "reranker_rank": None,
                        },
                    ],
                },
                "reranked": {
                    "mode_requested": "reranked", "mode_served": "reranked", "status": "ok",
                    "latency_ms": 1.0, "degradation_reasons": [],
                    "results": _arm_result(("e", "f"), rerank=True),
                },
            }
            path = write_arm_artifact(
                root=wave2 / "arms_root",
                run_id=wave2_run_id,
                case_id=case_id,
                query=case.query,
                cutoff_ts=case.cutoff,
                filters={
                    "ticker": case.ticker,
                    "evidence_types": [],
                    "source_classes": [],
                    "corpus_manifest_id": resolved.corpus_manifest_id,
                    "index_manifest_id": resolved.index_manifest_id,
                },
                retrieval_config={
                    "lexical_top_k": 20, "dense_top_k": 20, "fusion_k": 60,
                    "fused_top_k": 20, "display_top_k": 8,
                    "embedding_revision": BGE_M3_REVISION,
                    "reranker_revision": BGE_RERANKER_REVISION,
                    "reranker_timeout_seconds": 2.0,
                },
                arms=arms,
            )
            # Flatten into arms/ as case_id.json (four-arm layout).
            dest = wave2 / "arms" / f"{case_id}.json"
            dest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            pool = generate_union_pool(dest)
            write_union_pool(pool, wave2 / "pool" / f"{case_id}.json")
        # Pad/truncate to requested counts for negative tests.
        if arm_count < len(case_ids):
            for path in sorted((wave2 / "arms").glob("*.json"))[arm_count:]:
                path.unlink()
        if pool_count < len(case_ids):
            for path in sorted((wave2 / "pool").glob("*.json"))[pool_count:]:
                path.unlink()
        while arm_count > len(list((wave2 / "arms").glob("*.json"))):
            idx = len(list((wave2 / "arms").glob("*.json")))
            (wave2 / "arms" / f"extra-{idx}.json").write_text("{}")
        while pool_count > len(list((wave2 / "pool").glob("*.json"))):
            idx = len(list((wave2 / "pool").glob("*.json")))
            (wave2 / "pool" / f"extra-{idx}.json").write_text("{}")
    else:
        for idx in range(arm_count):
            (wave2 / "arms" / f"case-{idx}.json").write_text(
                json.dumps({"case_id": f"case-{idx}"})
            )
        for idx in range(pool_count):
            (wave2 / "pool" / f"case-{idx}.json").write_text(
                json.dumps({"case_id": f"case-{idx}"})
            )
    meta = {
        "schema_version": "post_import_run_meta_v1",
        "run_id": wave2_run_id if valid_artifacts else "wave2",
        "git_head": GIT_HEAD,
        "runtime_git_head": GIT_HEAD,
        "index_build_revision": CODE_REVISION,
        "snapshot_id": resolved.snapshot_id,
        "corpus_manifest_id": resolved.corpus_manifest_id,
        "source_bundle_id": resolved.source_bundle_id,
        "probe_report_id": resolved.probe_report_id,
        "postbuild_readiness_id": resolved.postbuild_readiness_id,
        "index_manifest_id": resolved.index_manifest_id,
        "active_table_name": resolved.active_table_name,
        "embedding_mode": embedding_mode,
        "case_count": case_count,
        "full_case_count": case_count,
        "db_sha256": resolved.db_sha256,
        "db_user_version": 13,
        "db_foreign_key_violations": 0,
    }
    if valid_artifacts:
        from catalyst_eval.post_import.four_arm import build_temporal_identity_validation

        meta["temporal_identity_validation"] = build_temporal_identity_validation(cases)
    if meta_extra:
        meta.update(meta_extra)
    (wave2 / "meta.json").write_text(json.dumps(meta, sort_keys=True))
    (wave2 / WAVE_TOKEN_FILENAME).write_text(token + "\n")
    return wave2


def _full_cases():
    return build_smoke_case_pack(GOLDEN_DIR)


def _prepare_runtime_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    init_trace_db(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ohlcv (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT
        )
        """
    )
    for ticker, trade_date in (
        ("TSLA", "2025-07-24"),
        ("TSLA", "2025-08-22"),
        ("GOOGL", "2025-06-12"),
    ):
        conn.execute(
            "INSERT INTO ohlcv VALUES (?, ?, 100, 101, 99, 100.5, 1000, 'fixture')",
            (ticker, trade_date),
        )
    conn.commit()
    conn.close()


def _build_frozen_corpus_db(path: Path) -> None:
    """Small read-only corpus DB whose corpus_chunks satisfy the served predicate."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE corpus_chunks (
            chunk_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            chunk_profile_version TEXT NOT NULL,
            section_key TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            content_text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            metadata_hash TEXT NOT NULL,
            source_class TEXT NOT NULL,
            available_at TEXT NOT NULL,
            ticker_associations TEXT NOT NULL,
            eligibility TEXT NOT NULL,
            manifest_id TEXT NOT NULL,
            status TEXT NOT NULL,
            boundary_kind TEXT,
            body_token_start INTEGER,
            body_token_end INTEGER,
            body_overlap_tokens INTEGER,
            prefix_token_count INTEGER,
            prefix_truncated INTEGER,
            section_parse_degraded INTEGER,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    manifest = EVIDENCE_KWARGS["corpus_manifest_id"]
    rows = []
    for idx, (chunk_id, ticker, available_at) in enumerate(
        [
            ("tsla-c1", "TSLA", "2025-07-23T00:00:00Z"),
            ("tsla-c2", "TSLA", "2025-07-23T00:00:00Z"),
            ("googl-c1", "GOOGL", "2025-06-11T00:00:00Z"),
        ],
        start=1,
    ):
        rows.append((
            chunk_id, f"doc:{chunk_id}", "news_v2", "body", idx,
            "earnings margin compression", hashlib.sha256(b"t").hexdigest(),
            hashlib.sha256(b"m").hexdigest(), "reported_news", available_at,
            json.dumps([ticker]), "eligible", manifest, "active",
            "paragraph", 0, 10, 0, 0, 0, 0, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
        ))
    conn.executemany(
        "INSERT INTO corpus_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


class _Response(HttpResponse):
    pass


def _decode(resp):
    try:
        return resp.json()
    except Exception:
        return None


class ClientSession:
    """Duck-typed HTTP session over FastAPI TestClient."""

    def __init__(self, client):
        self.client = client

    def get(self, path: str) -> HttpResponse:
        resp = self.client.get(path)
        return _Response(status_code=resp.status_code, json=_decode(resp), headers=dict(resp.headers))

    def post(self, path: str, json=None) -> HttpResponse:
        resp = self.client.post(path, json=json)
        return _Response(status_code=resp.status_code, json=_decode(resp), headers=dict(resp.headers))


class FakeLoader:
    def health(self):
        return {
            "status": "ready",
            "sqlite": {"status": "ready"},
            "lancedb": {"status": "ready"},
            "embedding": {"status": "ready"},
            "reranker": {"status": "ready"},
            "default_model": {"status": "ready", "model": "deepseek-v4-flash"},
            "errors": [],
        }


class RecordingCounter:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


class FakeGraph:
    def __init__(self, db_path, *, outcome):
        self.db_path = db_path
        self.outcome = outcome

    def invoke(self, state, run_id=None):
        outcome = self.outcome
        cost = outcome.get("cost_usd", 0.05)
        with TraceWriter(
            db_path=self.db_path,
            run_id=run_id,
            ticker=state.get("ticker"),
            trade_date=state.get("trade_date"),
            config="mcj_full",
        ) as writer:
            event_seq_by_node = {}
            abstain = outcome["output_status"] == "ABSTAIN"
            if abstain:
                # ABSTAIN path: no judge/validator; insufficient_handler terminal.
                nodes = [
                    "context_builder", "miner", "critic", "decision_router",
                    "insufficient_handler", "finalizer",
                ]
            else:
                nodes = [
                    "context_builder", "miner", "critic", "decision_router",
                    "judge", "validator", "finalizer",
                ]
            for node in nodes:
                status_after = outcome["output_status"] if node == "finalizer" else "RUNNING"
                event_seq = writer.event(
                    node=node,
                    started_at="2026-01-15T00:00:00Z",
                    ended_at="2026-01-15T00:00:01Z",
                    latency_ms=100,
                    model_id="deepseek-v4-flash",
                    input_tokens=10,
                    output_tokens=20,
                    cost_usd=cost / 5.0 if cost is not None else None,
                    decision=None,
                    error_type=None,
                    error_message=None,
                    status_before="RUNNING",
                    status_after=status_after,
                )
                event_seq_by_node[node] = event_seq
            causes = []
            for citation in outcome.get("citations", []):
                causes.append({
                    "text": "cause text",
                    "category": "earnings",
                    "confidence": 0.9,
                    "evidence_ids": [citation],
                    "direction": "down",
                })
            # Path-specific required (node, artifact_type) matrix (AMEND-5.1).
            def _art(node: str, artifact_type: str, payload: dict) -> None:
                seq = event_seq_by_node.get(node)
                if seq is None:
                    return
                write_node_artifact(
                    writer.conn, run_id=writer.run_id, event_seq=seq,
                    node=node, artifact_type=artifact_type, payload=payload,
                )

            _art("context_builder", "context_artifact", {"schema_version": "1.0.0"})
            _art("context_builder", "state_snapshot", {"state": {}})
            _art("miner", "retrieved_chunks", {"chunks": []})
            _art("miner", "reranked_chunks", {"chunks": []})
            _art("miner", "arm_b_evidence", {"per_asset": {}, "sha256": ""})
            _art("miner", "state_snapshot", {"state": {}})
            _art("critic", "graded_evidence", {"items": []})
            _art("critic", "all_graded_chunks", {"items": []})
            _art("critic", "critic_decision", {"decision": {"sufficiency": "sufficient"}})
            _art("critic", "raw_llm_response", {"text": "critic raw response"})
            _art("critic", "state_snapshot", {"state": {}})
            if not abstain:
                _art("judge", "judge_evidence", {"items": []})
                _art("judge", "raw_llm_response", {"text": "judge raw"})
                _art("judge", "state_snapshot", {"state": {}})
                # AMEND-7: judge_causes/judge_summary are validator-owned so the
                # persisted public artifacts reflect the post-filter state.
                _art("validator", "judge_causes", {"causes": causes})
                _art("validator", "judge_summary", {"summary": ""})
                _art("validator", "validator_decision", {"decision": "pass"})
                _art("validator", "raw_llm_response", {"text": "validator raw"})
                _art("validator", "state_snapshot", {"state": {}})
            else:
                _art("insufficient_handler", "state_snapshot", {"state": {}})
            _art("finalizer", "state_snapshot", {"state": {}})

            cutoff = outcome.get("cutoff", "2025-07-24T20:00:00Z")
            evidence_ids = list(outcome.get("citations", [])) or (["abstain-evidence-1"] if abstain else [])
            retrieved_chunks = [
                {
                    "asset_id": chunk_id,
                    "chunk_id": chunk_id,
                    "source_class": "reported_news",
                    "ticker_associations": [],
                    "corpus_manifest_id": EVIDENCE_KWARGS["corpus_manifest_id"],
                    "index_manifest_id": EVIDENCE_KWARGS["index_manifest_id"],
                    "available_at": cutoff,
                }
                for chunk_id in evidence_ids
            ]
            final_state = {
                "output_status": outcome["output_status"],
                "cutoff": cutoff,
                "context_cutoff": cutoff,
                "retrieval_cutoff": cutoff,
                "validator_cutoff": cutoff,
                "corpus_manifest_id": EVIDENCE_KWARGS["corpus_manifest_id"],
                "index_manifest_id": EVIDENCE_KWARGS["index_manifest_id"],
                "retrieved_chunks": retrieved_chunks,
                "context_artifact": {"schema_version": "1.0.0"},
                "hypotheses": [] if abstain else [
                    {
                        "cause_label": "earnings_guidance",
                        "prerequisite_gate_passed": outcome.get("gate_passed", True),
                        "prerequisite_gate_reason": outcome.get("gate_reason", "fixture pass"),
                    }
                ],
                "judge_evidence": None if abstain else {
                    citation: {"chunk_id": citation, "source_class": "reported_news"}
                    for citation in outcome.get("citations", [])
                },
                "retry_count": outcome.get("retry_count", 0),
                "repair_count": 0,
                "budget_exhausted": outcome.get("budget_exhausted", False),
                "is_degraded": outcome.get("is_degraded", False),
                "total_cost_usd": cost,
                "cost_breakdown": [],
                "error_type": None,
                "validation_error": None,
            }
            writer.complete(final_state)
        return {"output_status": outcome["output_status"], "summary_md": "done", "total_cost_usd": cost}


class _FakeGraphByTicker:
    def __init__(self, db_path, *, outcomes):
        self.db_path = db_path
        self.outcomes = outcomes

    def invoke(self, state, run_id=None):
        ticker = state.get("ticker")
        if ticker == "TSLA":
            trade_date = state.get("trade_date")
            outcome = self.outcomes["g007" if trade_date == "2025-08-22" else "g006"]
        else:
            outcome = self.outcomes["h004"]
        return FakeGraph(self.db_path, outcome=outcome).invoke(state, run_id=run_id)


def _make_app(db_path: Path, graph_factory, *, provider_counter: RecordingCounter | None = None):
    def factory(model=None, api_key=None):
        if provider_counter is not None:
            provider_counter()
        return graph_factory(db_path)

    service = LiveRunService(
        db_path=db_path,
        graph_factory=factory,
        credential_store=RuntimeCredentialStore(),
        timeout_seconds=60.0,
    )
    app = create_app(
        service_override=service,
        dependency_loader_override=FakeLoader(),
        workbench_store_override=WorkbenchStore(db_path=db_path),
    )
    return app, service


def _boundary() -> EmbeddingBoundary:
    return EmbeddingBoundary(
        embedding_mode="production_pinned",
        dimension=1024,
        model_revision="5617a9f61b028005a4858fdac845db406aefb181",
        tokenizer_revision="5617a9f61b028005a4858fdac845db406aefb181",
        is_mock=False,
        cuda_available=True,
    )


def _valid_outcomes():
    return {
        "g006": {
            "output_status": "SUFFICIENT", "cost_usd": 0.05,
            "cutoff": "2025-07-24T20:00:00Z", "citations": ["tsla-c1"],
        },
        "g007": {
            "output_status": "PARTIAL", "cost_usd": 0.04,
            "cutoff": "2025-08-22T20:00:00Z", "citations": ["tsla-c2"],
        },
        "h004": {
            "output_status": "ABSTAIN", "cost_usd": 0.01,
            "cutoff": "2025-06-12T20:00:00Z", "citations": [],
        },
    }


def _default_run_kwargs(
    *,
    tmp_path: Path,
    http,
    resolved=None,
    cases=None,
    wave2_dir=None,
    t4_evidence_dir=None,
    runtime_db_path=None,
    frozen_db_path=None,
    **overrides,
) -> dict:
    resolved = resolved if resolved is not None else _resolved()
    cases = cases if cases is not None else _full_cases()
    runtime_db_path = runtime_db_path if runtime_db_path is not None else tmp_path / "runtime.db"
    frozen_db_path = frozen_db_path if frozen_db_path is not None else tmp_path / "frozen.db"
    if wave2_dir is None:
        wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved)
    if t4_evidence_dir is None:
        t4_evidence_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    kwargs = dict(
        cases=cases,
        run_id="usmoke_test",
        output_root=tmp_path / "reports",
        resolved=resolved,
        wave2_dir=wave2_dir,
        t4_evidence_dir=t4_evidence_dir,
        boundary=_boundary(),
        runtime_db_path=runtime_db_path,
        frozen_db_path=frozen_db_path,
        http=http,
        provider="deepseek",
        model_id="deepseek-v4-flash",
        credential_source="server_env",
        poll_interval_seconds=0.01,
        poll_timeout_seconds=5.0,
        cost_ceiling_usd=COST_CEILING_USD,
        provider_validator=lambda: None,
    )
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# Stable case selection
# ---------------------------------------------------------------------------


def test_default_model_id_is_deepseek_v4_flash():
    from catalyst_eval.post_import.user_smoke import DEFAULT_MODEL_ID

    assert DEFAULT_MODEL_ID == "deepseek-v4-flash"


def test_retired_model_alias_rejected_before_provider_call(tmp_path):
    """deepseek-chat is retired; the runner must fail closed before any provider call."""
    from catalyst_eval.post_import.user_smoke import run_user_smoke

    _, _, _, kwargs = _happy_run(tmp_path)
    calls: list[str] = []

    def provider_validator():
        calls.append("provider-validator-called")

    kwargs["model_id"] = "deepseek-chat"
    kwargs["provider_validator"] = provider_validator
    with pytest.raises(ValueError, match="retired"):
        run_user_smoke(**kwargs)
    assert calls == []
    assert not (tmp_path / "reports" / "usmoke_test").exists()


def test_select_user_smoke_cases_selects_exactly_approved_three():
    full = _full_cases()
    selected = select_user_smoke_cases(full)
    assert [item.case.case_id for item in selected] == list(USER_SMOKE_CASE_IDS)
    classes = {item.case.case_id: item.expected_class for item in selected}
    assert classes == dict(USER_SMOKE_EXPECTED_CLASSES)


def test_select_user_smoke_cases_preserves_h004_query_override():
    full = _full_cases()
    h004 = next(item for item in select_user_smoke_cases(full) if item.case.case_id == "h004")
    golden_h004 = next(
        row for row in json.loads((GOLDEN_DIR / "h_refusal_cases.validated.json").read_text())
        if row.get("id") == "h004"
    )
    assert h004.case.query == golden_h004["query_override"]
    assert h004.raw_expected_status == "INSUFFICIENT"
    assert h004.expected_class == "ABSTAIN"


def test_select_user_smoke_cases_rejects_missing_case():
    full = _full_cases()
    missing = [case for case in full if case.case_id != "g007"]
    with pytest.raises(ValueError, match="g007"):
        select_user_smoke_cases(missing)


def test_select_user_smoke_cases_rejects_duplicate_case():
    full = _full_cases()
    duplicate = full + [case for case in full if case.case_id == "g006"]
    with pytest.raises(ValueError, match="duplicate"):
        select_user_smoke_cases(duplicate)


# ---------------------------------------------------------------------------
# Wave 2 evidence validation
# ---------------------------------------------------------------------------


def test_validate_wave2_evidence_accepts_valid_directories(tmp_path):
    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved)
    validated = validate_wave2_evidence(
        wave2_dir=wave2_dir,
        t4_evidence_dir=t4_dir,
        current_case_pack=cases,
        resolved=resolved,
    )
    assert isinstance(validated, ValidatedT4Evidence) or validated.four_arm_token == WAVE2_FOUR_ARM_TOKEN
    assert validated.four_arm_token == WAVE2_FOUR_ARM_TOKEN
    assert isinstance(validated.t4_evidence, ValidatedT4Evidence)


def test_validate_wave2_evidence_rejects_missing_four_arm_token(tmp_path):
    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved)
    (wave2_dir / WAVE_TOKEN_FILENAME).unlink()
    with pytest.raises(ValueError, match=WAVE_TOKEN_FILENAME):
        validate_wave2_evidence(
            wave2_dir=wave2_dir,
            t4_evidence_dir=t4_dir,
            current_case_pack=cases,
            resolved=resolved,
        )


def test_validate_wave2_evidence_rejects_forgeable_token(tmp_path):
    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved, token="CALLER_FORGED_TOKEN")
    with pytest.raises(ValueError, match=WAVE2_FOUR_ARM_TOKEN):
        validate_wave2_evidence(
            wave2_dir=wave2_dir,
            t4_evidence_dir=t4_dir,
            current_case_pack=cases,
            resolved=resolved,
        )


def test_validate_wave2_evidence_rejects_partial_or_limit_meta(tmp_path):
    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    for broken in (
        {"case_count": 1, "full_case_count": 10},
        {"embedding_mode": "mock_unit_test"},
        {"runtime_git_head": "0" * 40},
    ):
        wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved, meta_extra=broken)
        with pytest.raises(ValueError):
            validate_wave2_evidence(
                wave2_dir=wave2_dir,
                t4_evidence_dir=t4_dir,
                current_case_pack=cases,
                resolved=resolved,
            )


def test_validate_wave2_evidence_rejects_missing_arms_or_pools(tmp_path):
    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved, arm_count=9, pool_count=9)
    with pytest.raises(ValueError, match="arms"):
        validate_wave2_evidence(
            wave2_dir=wave2_dir,
            t4_evidence_dir=t4_dir,
            current_case_pack=cases,
            resolved=resolved,
        )


# ---------------------------------------------------------------------------
# Redaction / secret hygiene
# ---------------------------------------------------------------------------


def test_redact_payload_removes_api_keys_and_authorization():
    payload = {
        "authorization": "Bearer sk-live-1234567890abcdef",
        "api_key": "sk-abc123",
        "headers": {"x-api-key": "sk-xyz", "cookie": "session=abc"},
        "nested": {"deepseek_api_key": "sk-nested", "keep": "ok"},
        "message": "error with sk-live-1234567890abcdef inside text",
    }
    redacted = _redact_payload(payload)
    blob = json.dumps(redacted)
    assert "sk-live-1234567890abcdef" not in blob
    assert "sk-abc123" not in blob
    assert "sk-xyz" not in blob
    assert "sk-nested" not in blob
    assert redacted["nested"]["keep"] == "ok"


def test_secret_scan_rejects_planted_secret(tmp_path):
    evidence = tmp_path / "evidence"
    (evidence / "responses").mkdir(parents=True)
    (evidence / "responses" / "r.json").write_text(json.dumps({"text": "Bearer sk-planted-abcdef"}))
    violations = _secret_scan(evidence, secret_values=["sk-planted-abcdef"])
    assert violations


def test_secret_scan_accepts_clean_evidence(tmp_path):
    evidence = tmp_path / "evidence"
    (evidence / "responses").mkdir(parents=True)
    (evidence / "responses" / "r.json").write_text(json.dumps({"text": "clean", "authorization": "[REDACTED]"}))
    violations = _secret_scan(evidence, secret_values=["sk-planted-abcdef"])
    assert not violations


# ---------------------------------------------------------------------------
# HTTP integration
# ---------------------------------------------------------------------------


def _happy_app(tmp_path, runtime_db, frozen_db, *, outcomes=None, provider_counter=None):
    outcomes = outcomes if outcomes is not None else _valid_outcomes()
    app, _ = _make_app(
        runtime_db,
        lambda db_path: _FakeGraphByTicker(db_path, outcomes=outcomes),
        provider_counter=provider_counter,
    )
    return app


def _happy_run(tmp_path, *, outcomes=None, provider_counter=None, **overrides):
    from fastapi.testclient import TestClient

    runtime_db = tmp_path / "runtime.db"
    frozen_db = tmp_path / "frozen.db"
    _prepare_runtime_db(runtime_db)
    _build_frozen_corpus_db(frozen_db)
    resolved = _resolved(db_path=frozen_db)
    cases = _full_cases()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved)
    app = _happy_app(tmp_path, runtime_db, frozen_db, outcomes=outcomes, provider_counter=provider_counter)
    http = ClientSession(TestClient(app))
    kwargs = _default_run_kwargs(
        tmp_path=tmp_path,
        http=http,
        resolved=resolved,
        cases=cases,
        wave2_dir=wave2_dir,
        t4_evidence_dir=t4_dir,
        runtime_db_path=runtime_db,
        frozen_db_path=frozen_db,
    )
    kwargs.update(overrides)
    return runtime_db, frozen_db, resolved, kwargs


def test_run_user_smoke_http_happy_path_writes_token(tmp_path):
    runtime_db, frozen_db, resolved, kwargs = _happy_run(tmp_path)
    summary = run_user_smoke(**kwargs)
    assert isinstance(summary, UserSmokeSummary)
    assert summary.token_written is True
    final_dir = tmp_path / "reports" / "usmoke_test"
    assert final_dir.is_dir()
    token = (final_dir / WAVE_TOKEN_FILENAME).read_text().strip()
    assert token == USER_SMOKE_TOKEN
    assert (final_dir / "meta.json").is_file()
    assert (final_dir / "case_results.json").is_file()
    assert (final_dir / "checksums.sha256").is_file()
    traces = list((final_dir / "traces").glob("*.json"))
    assurance = list((final_dir / "assurance").glob("*.json"))
    assert len(traces) == 3
    assert len(assurance) == 3
    failure_results = summary.failure_path_results
    assert all(item.ok for item in failure_results)
    assert all(item.provider_calls == 0 for item in failure_results)
    case_results = json.loads((final_dir / "case_results.json").read_text())
    by_id = {row["case_id"]: row for row in case_results["cases"]}
    assert by_id["g006"]["output_status"] == "SUFFICIENT"
    assert by_id["g007"]["output_status"] == "PARTIAL"
    assert by_id["h004"]["output_status"] == "ABSTAIN"
    assert all(row["match"] for row in case_results["cases"])


def test_run_user_smoke_checksum_manifest_covers_evidence(tmp_path):
    _, _, _, kwargs = _happy_run(tmp_path)
    summary = run_user_smoke(**kwargs)
    assert summary.token_written is True
    final_dir = tmp_path / "reports" / "usmoke_test"
    manifest = {}
    for line in (final_dir / "checksums.sha256").read_text().splitlines():
        digest, _, rel = line.partition("  ")
        manifest[rel] = digest
    assert "checksums.sha256" not in manifest
    expected_files = {
        "meta.json",
        "case_results.json",
        "WAVE_TOKEN.txt",
        "requests/request-0.json",
        "responses/response-0.json",
        "traces/trace-g006.json",
        "assurance/assurance-g006.json",
        "failure_paths/failure-paths.json",
    }
    assert expected_files <= set(manifest)
    for rel, digest in manifest.items():
        path = final_dir / rel
        assert path.is_file()
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == digest


def test_run_user_smoke_gate_failure_before_provider_call(tmp_path):
    from fastapi.testclient import TestClient

    runtime_db = tmp_path / "runtime.db"
    frozen_db = tmp_path / "frozen.db"
    _prepare_runtime_db(runtime_db)
    _build_frozen_corpus_db(frozen_db)
    resolved = _resolved(db_path=frozen_db)
    cases = _full_cases()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    # Broken Wave 2 directory: the success token was removed by an attacker.
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved)
    (wave2_dir / WAVE_TOKEN_FILENAME).unlink()

    provider_counter = RecordingCounter()
    app = _happy_app(tmp_path, runtime_db, frozen_db, provider_counter=provider_counter)
    http = ClientSession(TestClient(app))

    with pytest.raises(ValueError):
        run_user_smoke(**_default_run_kwargs(
            tmp_path=tmp_path,
            http=http,
            resolved=resolved,
            cases=cases,
            wave2_dir=wave2_dir,
            t4_evidence_dir=t4_dir,
            runtime_db_path=runtime_db,
            frozen_db_path=frozen_db,
        ))

    assert provider_counter.calls == 0
    assert not (tmp_path / "reports" / "usmoke_test").exists()
    assert not list((tmp_path / "reports").glob(".usmoke_test.staging-*"))


class _ExplodingSession:
    """HTTP session that raises on a matching path (simulates transport failure)."""

    def __init__(self, inner, explode_on: str):
        self.inner = inner
        self.explode_on = explode_on

    def get(self, path: str) -> HttpResponse:
        if self.explode_on in path:
            raise RuntimeError("transport exploded")
        return self.inner.get(path)

    def post(self, path: str, json=None) -> HttpResponse:
        return self.inner.post(path, json=json)


def test_run_user_smoke_hard_failure_leaves_no_final_dir(tmp_path):
    from fastapi.testclient import TestClient

    runtime_db = tmp_path / "runtime.db"
    frozen_db = tmp_path / "frozen.db"
    _prepare_runtime_db(runtime_db)
    _build_frozen_corpus_db(frozen_db)
    resolved = _resolved(db_path=frozen_db)
    cases = _full_cases()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved)
    app = _happy_app(tmp_path, runtime_db, frozen_db)
    inner = ClientSession(TestClient(app))
    kwargs = _default_run_kwargs(
        tmp_path=tmp_path,
        http=inner,
        resolved=resolved,
        cases=cases,
        wave2_dir=wave2_dir,
        t4_evidence_dir=t4_dir,
        runtime_db_path=runtime_db,
        frozen_db_path=frozen_db,
    )
    kwargs["http"] = _ExplodingSession(inner, explode_on="/events")

    with pytest.raises(RuntimeError, match="transport exploded"):
        run_user_smoke(**kwargs)

    assert not (tmp_path / "reports" / "usmoke_test").exists()
    assert not list((tmp_path / "reports").glob(".usmoke_test.staging-*"))


def test_run_user_smoke_provider_crash_fails_closed_no_token(tmp_path):
    """A provider crash surfaces as typed FAILED_SYSTEM; evidence preserved, no token."""
    runtime_db, frozen_db, resolved, kwargs = _happy_run(tmp_path)

    def boom_graph(db_path):
        def invoke(state, run_id=None):
            raise RuntimeError("provider exploded")
        return type("BoomGraph", (), {"invoke": invoke})()

    from fastapi.testclient import TestClient

    app, _ = _make_app(runtime_db, boom_graph)
    kwargs["http"] = ClientSession(TestClient(app))
    kwargs["provider_validator"] = lambda: None

    summary = run_user_smoke(**kwargs)
    assert summary.token_written is False
    final_dir = tmp_path / "reports" / "usmoke_test"
    assert final_dir.is_dir()
    assert not (final_dir / WAVE_TOKEN_FILENAME).exists()
    assert not list((tmp_path / "reports").glob(".usmoke_test.staging-*"))


def test_run_user_smoke_expectation_mismatch_writes_no_token(tmp_path):
    outcomes = _valid_outcomes()
    outcomes["g006"] = {
        "output_status": "ABSTAIN", "cost_usd": 0.05,
        "cutoff": "2025-07-24T20:00:00Z", "citations": [],
    }
    _, _, _, kwargs = _happy_run(tmp_path, outcomes=outcomes)
    summary = run_user_smoke(**kwargs)
    assert summary.token_written is False
    final_dir = tmp_path / "reports" / "usmoke_test"
    assert final_dir.is_dir()  # evidence is preserved for review
    assert not (final_dir / WAVE_TOKEN_FILENAME).exists()
    case_results = json.loads((final_dir / "case_results.json").read_text())
    g006 = next(row for row in case_results["cases"] if row["case_id"] == "g006")
    assert g006["match"] is False


def test_run_user_smoke_unknown_cost_writes_no_token(tmp_path):
    outcomes = _valid_outcomes()
    outcomes["g006"] = {
        "output_status": "SUFFICIENT", "cost_usd": None,
        "cutoff": "2025-07-24T20:00:00Z", "citations": ["tsla-c1"],
    }
    _, _, _, kwargs = _happy_run(tmp_path, outcomes=outcomes)
    summary = run_user_smoke(**kwargs)
    assert summary.token_written is False
    final_dir = tmp_path / "reports" / "usmoke_test"
    assert final_dir.is_dir()
    assert not (final_dir / WAVE_TOKEN_FILENAME).exists()


def test_run_user_smoke_bounded_poll_timeout(tmp_path):
    from fastapi.testclient import TestClient

    runtime_db, frozen_db, resolved, kwargs = _happy_run(tmp_path)

    class StuckGraph:
        def invoke(self, state, run_id=None):
            import time
            time.sleep(30)

    app, _ = _make_app(runtime_db, lambda db_path: StuckGraph())
    kwargs["http"] = ClientSession(TestClient(app))
    kwargs["poll_timeout_seconds"] = 0.3

    with pytest.raises(ValueError, match="poll"):
        run_user_smoke(**kwargs)

    assert not (tmp_path / "reports" / "usmoke_test").exists()


def test_failure_paths_through_http_with_zero_provider_calls(tmp_path):
    provider_counter = RecordingCounter()
    _, _, _, kwargs = _happy_run(tmp_path, provider_counter=provider_counter)
    summary = run_user_smoke(**kwargs)
    assert summary.token_written is True
    names = {item.name for item in summary.failure_path_results}
    assert {"invalid_ticker", "invalid_trade_date", "missing_provider_credential", "model_validation_failure"} <= names
    for item in summary.failure_path_results:
        assert item.provider_calls == 0
    # Provider counter only fires for the three real case runs.
    assert provider_counter.calls == 3
    final_dir = tmp_path / "reports" / "usmoke_test"
    failure_paths = json.loads((final_dir / "failure_paths" / "failure-paths.json").read_text())
    assert all(entry["provider_calls"] == 0 for entry in failure_paths["results"])


def test_run_user_smoke_never_opens_frozen_db_writable(tmp_path, monkeypatch):
    import catalyst_eval.post_import.user_smoke as user_smoke

    _, _, _, kwargs = _happy_run(tmp_path)
    opened_uris: list[str] = []
    original_connect = sqlite3.connect

    def recording_connect(database, *args, **kwargs_):
        opened_uris.append(str(database))
        return original_connect(database, *args, **kwargs_)

    monkeypatch.setattr(user_smoke.sqlite3, "connect", recording_connect)

    summary = run_user_smoke(**kwargs)
    assert summary.token_written is True
    frozen_opens = [uri for uri in opened_uris if "frozen.db" in uri]
    assert frozen_opens, "the runner must open the frozen corpus DB read-only"
    assert all("mode=ro" in uri and "immutable=1" in uri for uri in frozen_opens)


def test_run_user_smoke_rejects_existing_final_run_id(tmp_path):
    _, _, _, kwargs = _happy_run(tmp_path)
    output_root = tmp_path / "reports"
    (output_root / "usmoke_test").mkdir(parents=True)

    with pytest.raises(ValueError, match="already exists"):
        run_user_smoke(**kwargs)


# ---------------------------------------------------------------------------
# Wave 3 citation validator: searchable-status contract (AMEND-5)
#
# The user-smoke citation check must accept metadata_only + eligible citations
# (searchable production evidence) while rejecting tombstoned, ineligible,
# wrong-ticker, and look-ahead citations.
# ---------------------------------------------------------------------------


def _citation_case(*, ticker: str = "TSLA", cutoff: str = "2025-07-24T20:00:00Z") -> CasePackCase:
    from catalyst_eval.post_import.case_pack import CasePackCase, SCHEMA_VERSION as CV

    return CasePackCase(
        schema_version=CV,
        case_id="g006",
        ticker=ticker,
        session_date="2025-07-24",
        cutoff=cutoff,
        query="Why did TSLA move on 2025-07-24?",
        source_set="fixture",
        golden={"golden_id": "g006", "expected_status": "SUFFICIENT"},
    )


def _citation_db(tmp_path, *, status="active", eligibility="eligible",
                 ticker="TSLA", available_at="2025-07-23T00:00:00Z") -> Path:
    from catalyst_eval.post_import.user_smoke import _validate_citations

    db = tmp_path / "frozen_citations.db"
    _build_frozen_corpus_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        """UPDATE corpus_chunks
           SET status = ?, eligibility = ?, ticker_associations = ?, available_at = ?
           WHERE chunk_id = 'tsla-c1'""",
        (status, eligibility, f'["{ticker}"]', available_at),
    )
    conn.commit()
    conn.close()
    return db


def test_citation_validator_accepts_metadata_only_eligible(tmp_path):
    from catalyst_eval.post_import.user_smoke import _validate_citations

    db = _citation_db(tmp_path, status="metadata_only")
    ok, detail = _validate_citations(
        db, case=_citation_case(), cited_ids=["tsla-c1"],
        manifest_id=EVIDENCE_KWARGS["corpus_manifest_id"],
    )
    assert ok, detail
    assert detail == "citations resolve"


def test_citation_validator_rejects_tombstoned(tmp_path):
    from catalyst_eval.post_import.user_smoke import _validate_citations

    db = _citation_db(tmp_path, status="tombstoned")
    ok, detail = _validate_citations(
        db, case=_citation_case(), cited_ids=["tsla-c1"],
        manifest_id=EVIDENCE_KWARGS["corpus_manifest_id"],
    )
    assert ok is False
    assert "unresolvable" in detail


def test_citation_validator_rejects_ineligible(tmp_path):
    from catalyst_eval.post_import.user_smoke import _validate_citations

    db = _citation_db(tmp_path, eligibility="ineligible")
    ok, detail = _validate_citations(
        db, case=_citation_case(), cited_ids=["tsla-c1"],
        manifest_id=EVIDENCE_KWARGS["corpus_manifest_id"],
    )
    assert ok is False
    assert "unresolvable" in detail


def test_citation_validator_rejects_wrong_ticker(tmp_path):
    from catalyst_eval.post_import.user_smoke import _validate_citations

    db = _citation_db(tmp_path, ticker="GOOGL")
    ok, detail = _validate_citations(
        db, case=_citation_case(ticker="TSLA"), cited_ids=["tsla-c1"],
        manifest_id=EVIDENCE_KWARGS["corpus_manifest_id"],
    )
    assert ok is False
    assert "unresolvable" in detail


def test_citation_validator_rejects_look_ahead(tmp_path):
    from catalyst_eval.post_import.user_smoke import _validate_citations

    db = _citation_db(tmp_path, available_at="2025-07-25T00:00:00Z")
    ok, detail = _validate_citations(
        db, case=_citation_case(), cited_ids=["tsla-c1"],
        manifest_id=EVIDENCE_KWARGS["corpus_manifest_id"],
    )
    assert ok is False
    assert "tsla-c1" in detail


def test_validate_wave2_evidence_accepts_four_arm_meta_without_db_sha256(tmp_path):
    """The four-arm run meta does not carry db_sha256 (by design); DB identity
    is bound through the T4 evidence and resolved runtime identity."""
    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(
        tmp_path, resolved=resolved,
        meta_extra={"db_sha256": None},
    )
    # Simulate the actual four-arm meta by removing the db_sha256 key.
    meta = json.loads((wave2_dir / "meta.json").read_text())
    meta.pop("db_sha256", None)
    (wave2_dir / "meta.json").write_text(json.dumps(meta, sort_keys=True))
    validated = validate_wave2_evidence(
        wave2_dir=wave2_dir,
        t4_evidence_dir=t4_dir,
        current_case_pack=cases,
        resolved=resolved,
    )
    assert validated.four_arm_token == WAVE2_FOUR_ARM_TOKEN


# ── AMEND-5: Wave 3 assurance hardening ───────────────────────────────────────

def _assert_token_gate_reason(final_dir: Path, needle: str) -> list[str]:
    meta = json.loads((final_dir / "meta.json").read_text(encoding="utf-8"))
    reasons = meta.get("token_gate", {}).get("reasons", [])
    assert any(needle in reason for reason in reasons), reasons
    return reasons


def test_run_user_smoke_missing_assurance_record_rejects_token(tmp_path, monkeypatch):
    """A missing assurance record must reject USER_SMOKE_OK."""
    import catalyst_eval.post_import.user_smoke as user_smoke

    monkeypatch.setattr(user_smoke, "_read_assurance", lambda *a, **k: None)
    _runtime_db, _frozen_db, _resolved, kwargs = _happy_run(tmp_path)
    summary = run_user_smoke(**kwargs)
    assert summary.token_written is False
    assert not (tmp_path / "reports" / "usmoke_test" / WAVE_TOKEN_FILENAME).exists()
    _assert_token_gate_reason(tmp_path / "reports" / "usmoke_test", "assurance")


def test_run_user_smoke_failed_assurance_check_rejects_token(tmp_path):
    """Expected status match alone is insufficient: any failed assurance check
    rejects USER_SMOKE_OK and records the check name in meta.token_gate.reasons."""
    outcomes = _valid_outcomes()
    outcomes["g006"]["gate_passed"] = False
    _runtime_db, _frozen_db, _resolved, kwargs = _happy_run(tmp_path, outcomes=outcomes)
    summary = run_user_smoke(**kwargs)
    assert summary.token_written is False
    final_dir = tmp_path / "reports" / "usmoke_test"
    assert not (final_dir / WAVE_TOKEN_FILENAME).exists()
    reasons = _assert_token_gate_reason(final_dir, "assurance")
    assert any("prerequisite_gates" in reason for reason in reasons)


def test_run_user_smoke_not_applicable_assurance_checks_do_not_block(tmp_path):
    """not_applicable assurance checks (no-hypothesis ABSTAIN) do not fail the run."""
    _runtime_db, _frozen_db, _resolved, kwargs = _happy_run(tmp_path)
    summary = run_user_smoke(**kwargs)
    assert summary.token_written is True
    final_dir = tmp_path / "reports" / "usmoke_test"
    assurance = json.loads((final_dir / "assurance" / "assurance-h004.json").read_text())
    by_name = {check["check_name"]: check["status"] for check in assurance["checks"]}
    assert by_name["judge_visibility"] == "not_applicable"
    assert by_name["prerequisite_gates"] == "not_applicable"


# ── AMEND-5: Wave 3 evidence completeness (automatic node artifacts) ──────────

REQUIRED_DIAGNOSTIC_TYPES = frozenset({
    "retrieved_chunks", "reranked_chunks", "arm_b_evidence",
    "graded_evidence", "all_graded_chunks", "critic_decision",
    "raw_llm_response", "state_snapshot",
})


def test_run_user_smoke_exports_node_artifacts(tmp_path):
    """The production runner automatically exports checksum-covered, redacted
    node artifacts for every case (no manual diagnostic step)."""
    _runtime_db, _frozen_db, _resolved, kwargs = _happy_run(tmp_path)
    summary = run_user_smoke(**kwargs)
    assert summary.token_written is True
    final_dir = tmp_path / "reports" / "usmoke_test"
    artifacts_dir = final_dir / "node_artifacts"
    assert artifacts_dir.is_dir()
    files = sorted(rel.as_posix() for rel in artifacts_dir.rglob("*") if rel.is_file())
    assert files, "node_artifacts export is empty"
    for case_id in USER_SMOKE_CASE_IDS:
        case_files = [
            rel for rel in files
            if f"/{case_id}/" in rel or rel.startswith(f"{case_id}/")
        ]
        assert case_files, f"missing node artifacts for {case_id}"
        blob = "\n".join(
            (artifacts_dir / rel).read_text(encoding="utf-8") for rel in case_files
        )
        present = {name for name in REQUIRED_DIAGNOSTIC_TYPES if f'"{name}"' in blob}
        assert present == REQUIRED_DIAGNOSTIC_TYPES, (
            f"{case_id} missing required artifact types: {sorted(REQUIRED_DIAGNOSTIC_TYPES - present)}"
        )
        assert "sk-" not in blob
        assert "authorization" not in blob.lower() or "[REDACTED]" in blob
        assert "/workspace/" not in blob
        assert "/Users/" not in blob
    manifest = (final_dir / "checksums.sha256").read_text()
    assert any("node_artifacts/by_case/" in line for line in manifest.splitlines())


def test_run_user_smoke_missing_required_artifact_fails_closed(tmp_path, monkeypatch):
    """A missing required (node, type) pair blocks the token and records
    case/node/type in meta.token_gate.reasons."""
    import catalyst_eval.post_import.user_smoke as user_smoke

    real_read = user_smoke._read_node_artifacts

    def dropping_read(db_path, run_id):
        rows = real_read(db_path, run_id)
        return [row for row in rows if row.get("artifact_type") != "reranked_chunks"]

    monkeypatch.setattr(user_smoke, "_read_node_artifacts", dropping_read)
    _runtime_db, _frozen_db, _resolved, kwargs = _happy_run(tmp_path)
    summary = run_user_smoke(**kwargs)
    assert summary.token_written is False
    final_dir = tmp_path / "reports" / "usmoke_test"
    assert final_dir.is_dir()
    assert not (final_dir / WAVE_TOKEN_FILENAME).exists()
    meta = json.loads((final_dir / "meta.json").read_text(encoding="utf-8"))
    reasons = meta["token_gate"]["reasons"]
    assert any("incomplete node artifact evidence" in reason for reason in reasons)
    assert any("reranked_chunks" in reason for reason in reasons)
    assert any("/miner/reranked_chunks" in reason for reason in reasons)


def test_validate_wave2_rejects_legacy_four_arm_full_wave23_final3():
    """Old Wave 2 evidence without effect_metrics / schema 1.1.0 must fail closed."""
    from pathlib import Path

    legacy = Path("data/run_reports/post_import/four_arm_full_wave23_final3")
    if not legacy.is_dir():
        pytest.skip("legacy evidence directory not present")
    t4 = Path("data/run_reports/post_import/t4_wave23_final3")
    if not t4.is_dir():
        pytest.skip("t4 evidence directory not present")
    with pytest.raises(ValueError):
        validate_wave2_evidence(
            wave2_dir=legacy,
            t4_evidence_dir=t4,
            current_case_pack=None,
            resolved=None,
        )


def test_validate_wave2_rejects_null_reranker_scores(tmp_path):
    """Wave3 must fail closed when production reranked hits lack scores/ranks."""
    from catalyst_data.retrieval.artifacts import compute_arm_artifact_id

    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved, cases=cases)
    # Tamper first arm: null all scores/ranks + matching provenance.
    arm_path = sorted((wave2_dir / "arms").glob("*.json"))[0]
    payload = json.loads(arm_path.read_text(encoding="utf-8"))
    for result in payload["arms"]["reranked"]["results"]:
        result["reranker_score"] = None
        result["reranker_rank"] = None
    for entry in payload["effect_metrics"]["reranker_provenance"]:
        entry["reranker_score"] = None
        entry["reranker_rank"] = None
    payload["artifact_id"] = compute_arm_artifact_id(payload)
    arm_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    # Also rebind pool.source_artifact_id so only score gate is under test.
    case_id = payload["case_id"]
    pool_path = wave2_dir / "pool" / f"{case_id}.json"
    if pool_path.is_file():
        pool = json.loads(pool_path.read_text(encoding="utf-8"))
        pool["source_artifact_id"] = payload["artifact_id"]
        pool_path.write_text(json.dumps(pool, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError, match="reranker_score|reload validation|effect-validity"):
        validate_wave2_evidence(
            wave2_dir=wave2_dir,
            t4_evidence_dir=t4_dir,
            current_case_pack=cases,
            resolved=resolved,
        )


def test_required_node_artifact_matrix_covers_paths():
    from catalyst_eval.post_import.user_smoke import required_node_artifact_pairs

    base = required_node_artifact_pairs(output_status="SUFFICIENT", entered_judge_validator=True)
    assert ("miner", "retrieved_chunks") in base
    assert ("judge", "judge_evidence") in base
    assert ("validator", "validator_decision") in base
    # AMEND-7: judge_causes/judge_summary are required on the validator node.
    assert ("validator", "judge_causes") in base
    assert ("validator", "judge_summary") in base
    assert ("judge", "judge_causes") not in base
    assert ("judge", "judge_summary") not in base
    abstain = required_node_artifact_pairs(output_status="ABSTAIN", entered_judge_validator=False)
    assert ("insufficient_handler", "state_snapshot") in abstain
    assert ("judge", "judge_evidence") not in abstain


# ── AMEND-5.2: Wave 3 effect-validity rejects non-finite reranker scores ─────

@pytest.mark.parametrize(
    "bad_score",
    [None, True, False, float("nan"), float("inf"), float("-inf")],
    ids=["None", "True", "False", "NaN", "+Inf", "-Inf"],
)
def test_effect_validity_rejects_non_finite_reranker_scores(bad_score):
    from catalyst_eval.post_import.user_smoke import _effect_validity_from_artifact

    # Bypass load validation by constructing a minimal ArmArtifact-like object.
    class _A:
        def __init__(self, payload):
            self.payload = payload
            self.arms = payload["arms"]

    payload = {
        "effect_metrics": {
            "lexical_count": 1, "dense_count": 1, "hybrid_count": 1, "reranked_count": 1,
            "hybrid_lexical_contribution": 1, "hybrid_dense_contribution": 1,
            "reranker_input_count": 1, "reranker_output_count": 1,
            "reranker_provenance": [{
                "chunk_id": "e", "rank": 1,
                "reranker_score": bad_score, "reranker_rank": 1,
            }],
        },
        "arms": {
            "fts5": {"status": "ok", "results": [{"chunk_id": "a"}]},
            "dense": {"status": "ok", "results": [{"chunk_id": "b"}]},
            "hybrid": {"status": "ok", "results": [{"chunk_id": "c", "lexical_rank": 1, "dense_rank": 1}]},
            "reranked": {
                "status": "ok", "mode_served": "reranked",
                "results": [{
                    "chunk_id": "e", "rank": 1,
                    "reranker_score": bad_score, "reranker_rank": 1,
                }],
            },
        },
    }
    problems = _effect_validity_from_artifact(_A(payload))
    assert any("reranker_score" in p or "finite" in p for p in problems), problems


@pytest.mark.parametrize(
    "bad_score",
    [None, True, False, float("nan"), float("inf"), float("-inf")],
    ids=["None", "True", "False", "NaN", "+Inf", "-Inf"],
)
def test_validate_wave2_rejects_non_finite_reranker_scores(tmp_path, bad_score):
    from catalyst_data.retrieval.artifacts import compute_arm_artifact_id

    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved, cases=cases)
    arm_path = sorted((wave2_dir / "arms").glob("*.json"))[0]
    payload = json.loads(arm_path.read_text(encoding="utf-8"))
    for result in payload["arms"]["reranked"]["results"]:
        result["reranker_score"] = bad_score
    for entry in payload["effect_metrics"]["reranker_provenance"]:
        entry["reranker_score"] = bad_score
    payload["artifact_id"] = compute_arm_artifact_id(payload)
    arm_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    case_id = payload["case_id"]
    pool_path = wave2_dir / "pool" / f"{case_id}.json"
    if pool_path.is_file():
        pool = json.loads(pool_path.read_text(encoding="utf-8"))
        pool["source_artifact_id"] = payload["artifact_id"]
        pool_path.write_text(json.dumps(pool, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError, match="reranker_score|reload validation|effect-validity|finite"):
        validate_wave2_evidence(
            wave2_dir=wave2_dir,
            t4_evidence_dir=t4_dir,
            current_case_pack=cases,
            resolved=resolved,
        )


# ── AMEND-5.2: Wave 2 semantic binding (query/ticker/cutoff/run_id/corpus/index)

def _tamper_first_arm(wave2_dir, mutator):
    from catalyst_data.retrieval.artifacts import compute_arm_artifact_id

    arm_path = sorted((wave2_dir / "arms").glob("*.json"))[0]
    payload = json.loads(arm_path.read_text(encoding="utf-8"))
    mutator(payload)
    payload["artifact_id"] = compute_arm_artifact_id(payload)
    arm_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    case_id = payload["case_id"]
    pool_path = wave2_dir / "pool" / f"{case_id}.json"
    if pool_path.is_file():
        pool = json.loads(pool_path.read_text(encoding="utf-8"))
        pool["source_artifact_id"] = payload["artifact_id"]
        pool_path.write_text(json.dumps(pool, sort_keys=True), encoding="utf-8")
    return payload


def test_validate_wave2_rejects_wrong_query_sha256(tmp_path):
    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved, cases=cases)

    def mut(payload):
        payload["query_sha256"] = "a" * 64

    _tamper_first_arm(wave2_dir, mut)
    with pytest.raises(ValueError, match="query_sha256|query hash"):
        validate_wave2_evidence(
            wave2_dir=wave2_dir, t4_evidence_dir=t4_dir,
            current_case_pack=cases, resolved=resolved,
        )


def test_validate_wave2_rejects_wrong_ticker(tmp_path):
    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved, cases=cases)

    def mut(payload):
        payload["filters"]["ticker"] = "ZZZZ"

    _tamper_first_arm(wave2_dir, mut)
    with pytest.raises(ValueError, match="ticker"):
        validate_wave2_evidence(
            wave2_dir=wave2_dir, t4_evidence_dir=t4_dir,
            current_case_pack=cases, resolved=resolved,
        )


def test_validate_wave2_rejects_wrong_cutoff_ts(tmp_path):
    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved, cases=cases)

    def mut(payload):
        payload["cutoff_ts"] = "1999-01-01T00:00:00Z"

    _tamper_first_arm(wave2_dir, mut)
    with pytest.raises(ValueError, match="cutoff"):
        validate_wave2_evidence(
            wave2_dir=wave2_dir, t4_evidence_dir=t4_dir,
            current_case_pack=cases, resolved=resolved,
        )


def test_validate_wave2_rejects_wrong_run_id(tmp_path):
    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved, cases=cases)

    def mut(payload):
        payload["run_id"] = "wrong-run-id"

    _tamper_first_arm(wave2_dir, mut)
    with pytest.raises(ValueError, match="run_id"):
        validate_wave2_evidence(
            wave2_dir=wave2_dir, t4_evidence_dir=t4_dir,
            current_case_pack=cases, resolved=resolved,
        )


def test_validate_wave2_rejects_wrong_corpus_manifest_id(tmp_path):
    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved, cases=cases)

    def mut(payload):
        payload["filters"]["corpus_manifest_id"] = "b" * 64

    _tamper_first_arm(wave2_dir, mut)
    with pytest.raises(ValueError, match="corpus_manifest_id|corpus"):
        validate_wave2_evidence(
            wave2_dir=wave2_dir, t4_evidence_dir=t4_dir,
            current_case_pack=cases, resolved=resolved,
        )


def test_validate_wave2_rejects_wrong_index_manifest_id(tmp_path):
    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved, cases=cases)

    def mut(payload):
        payload["filters"]["index_manifest_id"] = "c" * 64

    _tamper_first_arm(wave2_dir, mut)
    with pytest.raises(ValueError, match="index_manifest_id|index"):
        validate_wave2_evidence(
            wave2_dir=wave2_dir, t4_evidence_dir=t4_dir,
            current_case_pack=cases, resolved=resolved,
        )


# ── AMEND-5.2B: Wave 2 per-case temporal identity on persisted meta ──────────

def test_validate_wave2_rejects_missing_temporal_identity_validation(tmp_path):
    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved, cases=cases)
    meta_path = wave2_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("temporal_identity_validation", None)
    meta_path.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError, match="temporal_identity_validation"):
        validate_wave2_evidence(
            wave2_dir=wave2_dir, t4_evidence_dir=t4_dir,
            current_case_pack=cases, resolved=resolved,
        )


def test_validate_wave2_rejects_wrong_temporal_center(tmp_path):
    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved, cases=cases)
    meta_path = wave2_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    first_id = cases[0].case_id
    meta["temporal_identity_validation"][first_id]["temporal_center_date"] = "1999-01-01"
    meta_path.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError, match="temporal_center|temporal_identity"):
        validate_wave2_evidence(
            wave2_dir=wave2_dir, t4_evidence_dir=t4_dir,
            current_case_pack=cases, resolved=resolved,
        )


def test_validate_wave2_rejects_temporal_identity_case_mismatch(tmp_path):
    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved, cases=cases)
    meta_path = wave2_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    first_id = cases[0].case_id
    del meta["temporal_identity_validation"][first_id]
    meta_path.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError, match="temporal_identity|case"):
        validate_wave2_evidence(
            wave2_dir=wave2_dir, t4_evidence_dir=t4_dir,
            current_case_pack=cases, resolved=resolved,
        )


# ── AMEND-5.2C: Wave 2 preflight rejects every wrong temporal dimension ───────

@pytest.mark.parametrize(
    "field,value",
    [
        ("query_date", "1999-01-01"),
        ("query_date_conflict", True),
        ("query_date_decision", "none"),
    ],
)
def test_validate_wave2_rejects_wrong_temporal_identity_fields(tmp_path, field, value):
    cases = _full_cases()
    resolved = _resolved()
    t4_dir = _build_t4_evidence(tmp_path, cases=cases, resolved=resolved)
    wave2_dir = _build_wave2_evidence(tmp_path, resolved=resolved, cases=cases)
    meta_path = wave2_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    first_id = cases[0].case_id
    meta["temporal_identity_validation"][first_id][field] = value
    meta_path.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError, match="temporal_identity"):
        validate_wave2_evidence(
            wave2_dir=wave2_dir, t4_evidence_dir=t4_dir,
            current_case_pack=cases, resolved=resolved,
        )


# ── AMEND-5.2C P2: approved Wave 2 pack must not be wrongly short-circuited ──

class _StubRetriever:
    """Minimal miner retriever: records calls, returns no evidence."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def retrieve(self, query, *, ticker, cutoff, requested_manifest_id, top_k=8, candidate_depth=20):
        self.calls.append({
            "query": query, "ticker": ticker, "cutoff": cutoff,
            "requested_manifest_id": requested_manifest_id,
            "top_k": top_k, "candidate_depth": candidate_depth,
        })
        return ()


class _StubCutoffPolicy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def compute_cutoff(self, *, ticker, session_date, mode) -> str:
        self.calls.append((ticker, session_date, mode))
        return "2026-01-15T21:00:00Z"


def test_miner_approved_ten_case_pack_not_short_circuited():
    """Every manager-approved Wave 2 case query must keep the structured ticker
    consistent or fail-open through the provenance-aware miner — none may be
    wrongly hard-rejected as a foreign ticker mismatch."""
    from catalyst_agents.nodes.miner import miner

    cases = _full_cases()
    assert len(cases) == 10
    for case in cases:
        retriever = _StubRetriever()
        cutoff_policy = _StubCutoffPolicy()
        state = {
            "ticker": case.ticker,
            "trade_date": case.session_date,
            "query": case.query,
            "price_move_pct": None,
            "corpus_manifest_id": "corpus-fixture-v1",
        }
        out = miner(state, retriever=retriever, cutoff_policy=cutoff_policy)
        assert out.get("ticker_consistent") is not False, (case.case_id, case.query, out)
        assert retriever.calls, case.case_id
        if out.get("ticker_consistent") is True:
            assert out.get("query_ticker_raw") == case.ticker.upper(), case.case_id
        else:
            assert out.get("query_ticker_raw") is None, case.case_id
