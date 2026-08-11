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
) -> Path:
    resolved = resolved if resolved is not None else _resolved()
    wave2 = tmp_path / f"wave2_full_{uuid.uuid4().hex[:8]}"
    (wave2 / "arms").mkdir(parents=True)
    (wave2 / "pool").mkdir(parents=True)
    for idx in range(arm_count):
        (wave2 / "arms" / f"case-{idx}.json").write_text(json.dumps({"case_id": f"case-{idx}"}))
    for idx in range(pool_count):
        (wave2 / "pool" / f"case-{idx}.json").write_text(json.dumps({"case_id": f"case-{idx}"}))
    meta = {
        "schema_version": "post_import_run_meta_v1",
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
            "default_model": {"status": "ready", "model": "deepseek-chat"},
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
            for node in ("critic", "decision_router", "judge", "validator", "finalizer"):
                status_after = outcome["output_status"] if node == "finalizer" else "RUNNING"
                event_seq = writer.event(
                    node=node,
                    started_at="2026-01-15T00:00:00Z",
                    ended_at="2026-01-15T00:00:01Z",
                    latency_ms=100,
                    model_id="deepseek-chat",
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
            write_node_artifact(
                writer.conn,
                run_id=writer.run_id,
                event_seq=event_seq_by_node["judge"],
                node="judge",
                artifact_type="judge_causes",
                payload={"causes": causes},
            )
            final_state = {
                "output_status": outcome["output_status"],
                "cutoff": outcome.get("cutoff", "2025-07-24T20:00:00Z"),
                "corpus_manifest_id": EVIDENCE_KWARGS["corpus_manifest_id"],
                "index_manifest_id": EVIDENCE_KWARGS["index_manifest_id"],
                "retrieved_chunks": [],
                "hypotheses": [],
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
        model_id="deepseek-chat",
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
