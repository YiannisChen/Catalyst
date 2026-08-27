"""M5-10 executable gates (sealed baseline integrity + semantic ontology regression).

M5 plan §5; Final TSD §27 M5. Two deterministic, repo-executable gates — no
real-provider comparison, no path-bound /root evidence derivation, no quality
or comparable claims.

Gate A verifies the sealed M1 corrective baseline artifact chain without
running any model. Gate B is a deterministic fixture-only regression of the
semantic ontology boundary over the approved 10-case T4 pack built by the repo
case-pack code, comparing only mechanical parity metrics.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from catalyst_eval.post_import.case_pack import (
    build_smoke_case_pack,
    compute_case_pack_id,
)
from catalyst_eval.post_import.t4_evidence import (
    APPROVED_T4_CASE_COUNT,
    APPROVED_T4_CASE_PACK_ID,
    APPROVED_T4_ORDERED_CASE_IDS,
)

GATE_A_SCHEMA = "v1_1_m5_gate_a_v1"
GATE_B_SCHEMA = "v1_1_m5_gate_b_v1"

SEALED_BASELINE_TAG = "v1.1-m1-baseline-corrective-seal"
SEALED_BASELINE_REPORT = (
    "data/baseline/reports/v1_1_baseline_621375bc_corrective_seal.json"
)
SEALED_BASELINE_SHA256 = (
    "862192eba92048ad9859260d082a027a29ffec5c836bd72768ec60ac041af0c7"
)
SEALED_BASELINE_SCHEMA = "baseline_v1"
FOUR_ARM_TOKEN = "FOUR_ARM_E2E_OK"
USER_SMOKE_TOKEN = "USER_SMOKE_OK"
Q002_PROMOTED_ENV_REASON = "q_002_promoted_environment_tuple_unrecovered"


class GateFailure(Exception):
    """Hard gate failure: no artifact is written and the CLI exits 1."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(args: list[str], repo_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )


def _verify_tag(tag: str, repo_root: Path) -> None:
    """The tag must exist (annotated) and be an ancestor of the M5 HEAD."""
    exists = _git(["tag", "-l", tag], repo_root)
    if tag not in exists.stdout.split():
        raise GateFailure(f"sealed tag {tag!r} does not exist")
    object_type = _git(["cat-file", "-t", tag], repo_root)
    if object_type.stdout.strip() != "tag":
        raise GateFailure(f"sealed tag {tag!r} is not annotated")
    ancestor = _git(["merge-base", "--is-ancestor", tag, "HEAD"], repo_root)
    if ancestor.returncode != 0:
        raise GateFailure(f"sealed tag {tag!r} is not an ancestor of HEAD")


def _verify_report(report_path: Path) -> dict[str, Any]:
    if not report_path.is_file():
        raise GateFailure(f"sealed report not found: {report_path}")
    actual_sha = _sha256_file(report_path)
    if actual_sha != SEALED_BASELINE_SHA256:
        raise GateFailure(
            f"sealed report SHA-256 mismatch: {actual_sha} != {SEALED_BASELINE_SHA256}"
        )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GateFailure(f"sealed report is not valid JSON: {exc}") from exc
    if report.get("schema_version") != SEALED_BASELINE_SCHEMA:
        raise GateFailure(
            f"sealed report schema mismatch: {report.get('schema_version')!r}"
        )
    runs = report.get("runs")
    if not isinstance(runs, list):
        raise GateFailure("sealed report runs metadata is missing")
    by_token = {run.get("success_token"): run for run in runs}
    missing = [token for token in (FOUR_ARM_TOKEN, USER_SMOKE_TOKEN) if token not in by_token]
    if missing:
        raise GateFailure(f"sealed report missing run tokens: {missing}")
    for token in (FOUR_ARM_TOKEN, USER_SMOKE_TOKEN):
        run = by_token[token]
        sha = run.get("success_token_sha256")
        if not isinstance(sha, str) or len(sha) != 64:
            raise GateFailure(f"run token {token} has no valid success_token_sha256")
    comparability = report.get("comparability") or {}
    if comparability.get("promoted_env_recovered") is not False:
        raise GateFailure("Q-002 NON-COMPARABLE must preserve promoted_env_recovered=false")
    if comparability.get("promoted_env_reason") != Q002_PROMOTED_ENV_REASON:
        raise GateFailure("Q-002 NON-COMPARABLE promoted_env_reason mismatch")
    return {
        "report_schema": report["schema_version"],
        "run_tokens": {
            "four_arm": {
                "token": FOUR_ARM_TOKEN,
                "success_token_sha256": by_token[FOUR_ARM_TOKEN]["success_token_sha256"],
            },
            "user_smoke": {
                "token": USER_SMOKE_TOKEN,
                "success_token_sha256": by_token[USER_SMOKE_TOKEN]["success_token_sha256"],
            },
        },
        "q002_non_comparable": {
            "promoted_env_recovered": comparability["promoted_env_recovered"],
            "promoted_env_reason": comparability["promoted_env_reason"],
        },
    }


def sealed_baseline_integrity_gate(
    *,
    repo_root: Path,
    tag: str,
    report_path: Path,
    out_path: Path,
) -> dict[str, Any]:
    """Gate A: verify the sealed baseline artifact chain; write the artifact."""
    _verify_tag(tag, repo_root)
    verified = _verify_report(Path(report_path))
    payload = {
        "schema_version": GATE_A_SCHEMA,
        "tag_verified": True,
        "report_path": str(report_path),
        "report_sha256": SEALED_BASELINE_SHA256,
        "report_schema": verified["report_schema"],
        "run_tokens": verified["run_tokens"],
        "q002_non_comparable": verified["q002_non_comparable"],
        "exit_status": 0,
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


# ---------------------------------------------------------------------------
# Gate B: real sealed-legacy reader + real V1 fixture semantic path
# ---------------------------------------------------------------------------

_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|credential|authorization)\b|"
    r"\bsk-[a-z0-9_-]{8,}"
)

_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")

_GATE_B_METRICS = (
    "ticker_cutoff_violations",
    "citation_resolution",
    "runtime_identity_binding",
    "context_pack_identity",
    "claim_lineage",
    "secret_leakage",
)


def _case_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def extract_answer_markers(answer_text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract emitted citation and claim markers from answer text (same
    convention as catalyst_agents.graph.extract_answer_markers)."""
    citations = tuple(
        sorted(set(re.findall(r"\(([A-Za-z0-9:_-]+)\)", answer_text)))
    )
    claim_markers = tuple(
        sorted(set(re.findall(r"\[([A-Za-z0-9:_-]+)\]", answer_text)))
    )
    return citations, claim_markers


def _serialize(value: Any) -> str:
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except TypeError:  # pragma: no cover
        return str(value)


# --- deterministic fixture providers for the real V1 path --------------------

class _FixtureObservationProvider:
    """Deterministic observation provider bound to the case request facts."""

    def load_context_inputs(
        self, *, ticker, session_date, cutoff, information_window_start_at=None
    ):
        from datetime import date

        from catalyst_agents.attribution.provider import ContextInputs

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
            peer_returns_by_ticker={"MSFT": 9.8, "NVDA": 9.6},
        )


class _FixtureRetriever:
    """Serves one deterministic in-scope evidence item bound to the case."""

    def __init__(self, case, evidence_id, temporal_identity, runtime_identity):
        self.case = case
        self.evidence_id = evidence_id
        self.temporal_identity = temporal_identity
        self.runtime_identity = runtime_identity
        self.served_ticker: str | None = None
        self.served_cutoff: str | None = None

    def _evidence(self, chunk_id: str, rank: int):
        from catalyst_agents.attribution.provider import RetrievedEvidence

        return RetrievedEvidence(
            chunk_id=chunk_id,
            document_id=f"doc:{chunk_id}",
            content_text=f"evidence {chunk_id}",
            available_at="2026-01-15T10:00:00Z",
            source_class="issuer_disclosure",
            ticker_associations=(self.served_ticker or self.case.ticker,),
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
            canonical_asset_id=f"asset:{self.case.ticker}:{chunk_id}",
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
            eligible_at=self.temporal_identity.cutoff_at,
            temporal_identity=self.temporal_identity,
            data_runtime_identity=self.runtime_identity,
        )

    def retrieve(
        self, query, *, ticker, cutoff, requested_manifest_id,
        temporal_identity=None, top_k=8, candidate_depth=20,
    ):
        self.served_ticker = ticker
        self.served_cutoff = cutoff
        return (self._evidence(self.evidence_id, 1),)


class _FixtureAnalystProvider:
    """Deterministic Analyst with ONE policy for every case, derived only from
    ordinary public request inputs (the served evidence identity). It never
    branches on golden, source_set, expected class, refusal filenames, or
    case-ID prefix; hidden gold is never read and Gate B stays mechanical."""

    def __init__(self, case, evidence_id):
        self.case = case
        self.evidence_id = evidence_id
        self.calls = 0
        self.capability_metadata = {
            "supports_structured_output": True,
            "supports_true_streaming": True,
            "declares_token_accounting": True,
            "normalizes_timeout_errors": True,
            "capability_revision": "gate-b-v1",
        }

    def invoke(self, messages):
        self.calls += 1
        return self._decision()

    def _decision(self):
        return {
            "schema_version": "1.0",
            "evidence_decisions": [
                {
                    "evidence_id": self.evidence_id,
                    "disposition": "SUPPORT",
                    "supports_hypothesis_refs": ["h1"],
                    "reason_code": "material_support",
                }
            ],
            "candidate_hypotheses": [
                {
                    "hypothesis_ref": "h1",
                    "cause_type": "COMPANY_SPECIFIC_CATALYST",
                    "statement": "Deterministic fixture explanation.",
                    "supporting_evidence_ids": [self.evidence_id],
                    "magnitude_fit": "STRONG",
                    "proposed_role": "PRIMARY",
                }
            ],
            "research_decision": "READY",
            "recommended_status": "SUFFICIENT",
            "proposed_attribution_type": "EVIDENCE_BACKED_CAUSAL",
        }

    def with_structured_output(self, schema):
        outer = self

        class Surface:
            def invoke(self, messages):
                return outer.invoke(messages)

        return Surface()


class _FixtureWriterProvider:
    """Deterministic Writer echoing the prompt's claim/citation markers."""

    def __init__(self):
        self.calls = 0
        self.capability_metadata = {
            "supports_structured_output": True,
            "supports_true_streaming": True,
            "declares_token_accounting": True,
            "normalizes_timeout_errors": True,
            "capability_revision": "gate-b-v1",
        }

    def stream(self, messages):
        self.calls += 1
        prompt = messages[0]["content"] if isinstance(messages[0], dict) else messages[0].content
        if "fixed abstention" in prompt.lower():
            yield (
                "OBSERVED_MOVE\nDeterministic fixture move.\n"
                "LIMITATIONS\nNo causal explanation was established from the "
                "available evidence."
            )
            return
        claim_match = re.search(r"claim_id: ([A-Za-z0-9:_-]+)", prompt)
        claim_id = claim_match.group(1) if claim_match else "claim:unknown"
        citations_match = re.search(
            r"citations: ([A-Za-z0-9:_-]+(?:,[A-Za-z0-9:_-]+)*)", prompt
        )
        citations = citations_match.group(1) if citations_match else ""
        yield (
            "SUMMARY\nDeterministic fixture answer. "
            f"[{claim_id}] ({citations})\n"
            "CAUSAL_EXPLANATION\nFixture mechanism.\nLIMITATIONS\nNone."
        )


def _case_temporal_identity(case):
    from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _timezone

    from catalyst_data.canonical.temporal import TemporalIdentity

    cutoff_at = _datetime.fromisoformat(case.cutoff.replace("Z", "+00:00")).replace(
        tzinfo=_timezone.utc
    )
    return TemporalIdentity(
        session_date=case.session_date,
        market_timezone="America/New_York",
        session_open_at=cutoff_at.replace(hour=14, minute=30, second=0, microsecond=0),
        session_close_at=cutoff_at,
        information_window_start_at=cutoff_at - _timedelta(days=1),
        cutoff_at=cutoff_at,
    )


def _case_runtime_identity(case):
    from catalyst_data.canonical.identity import DataRuntimeIdentity

    return DataRuntimeIdentity(
        data_snapshot_id=_case_hash(case.case_id, "snapshot"),
        corpus_manifest_id=_case_hash(case.case_id, "corpus"),
        fts_index_version="build:fts",
        dense_index_version=_case_hash(case.case_id, "dense"),
        embedding_model_revision="emb:1",
        reranker_revision="rr:1",
        query_policy_version="qp:v1",
    )


def _case_policy():
    from catalyst_agents.runtime.manifest import ObservationPolicyConfig

    return ObservationPolicyConfig(
        material_target_return_pct=2.0,
        material_prior_return_pct=1.5,
        quiet_target_return_pct=0.5,
        flat_reference_return_pct=0.25,
        aligned_residual_pct=1.0,
        volume_elevated_ratio=1.5,
        volume_extreme_ratio=3.0,
        minimum_peer_count=2,
        require_sector_and_peer_for_broad_sector=True,
        scenario_policy_version="sp:v1",
    )


def _case_registry():
    from catalyst_agents.retrieval.corrective import (
        BackendCapability,
        BackendHealth,
        CorrectiveCapabilityRegistry,
    )
    from catalyst_agents.retrieval.task import EvidenceNeed

    capabilities = {}
    for name in (
        "COMPANY_PRIMARY", "COMPANY_NEWS", "SECTOR_NEWS", "MACRO_EVENT",
        "MACRO_SERIES", "FUNDAMENTALS",
    ):
        capabilities[EvidenceNeed(name)] = BackendCapability(
            backend=f"backend:{name.lower()}", health=BackendHealth.HEALTHY
        )
    return CorrectiveCapabilityRegistry(capabilities)


def v1_fixture_run(case: Any) -> dict[str, Any]:
    """Run the real M5 V1 semantic path for one case with deterministic fakes.

    Returns the raw artifact surface the gate derives metrics from: the
    evidence items actually served, the runtime identity actually bound, the
    persisted ContextPack hash, the normalized assessment, the validated claim
    plan, and the streamed answer text. Gold is hidden; no metric is computed
    here.
    """
    from catalyst_agents.graph import run_v1_graph
    from catalyst_agents.runtime.pack_persistence import InMemoryPackStore

    temporal = _case_temporal_identity(case)
    runtime = _case_runtime_identity(case)
    evidence_id = f"chunk:{case.case_id}"
    retriever = _FixtureRetriever(case, evidence_id, temporal, runtime)
    analyst = _FixtureAnalystProvider(case, evidence_id)
    writer = _FixtureWriterProvider()
    result = run_v1_graph(
        run_id=f"v1:{case.case_id}",
        temporal_identity=temporal,
        data_runtime_identity=runtime,
        ticker=case.ticker,
        cutoff=case.cutoff,
        requested_manifest_id=_case_hash(case.case_id, "manifest"),
        observation_provider=_FixtureObservationProvider(),
        retriever=retriever,
        policy_config=_case_policy(),
        research_concurrency=1,
        research_stage_timeout_seconds=30.0,
        persistence=InMemoryPackStore(),
        packing_policy_version="gate-b:v1",
        template_bytes=b"system: analyse\n",
        template_version="gate-b:prompt:v1",
        analyst_llm=analyst,
        writer_llm=writer,
        capability_registry=_case_registry(),
        prompt_template="You are the Evidence Analyst. Emit the strict schema.",
    )
    return {
        "case_id": case.case_id,
        "ticker": case.ticker,
        "cutoff": case.cutoff,
        "retrieval_cutoff": retriever.served_cutoff,
        "evidence_items": [
            {
                "evidence_id": item.evidence_id,
                "ticker": retriever.served_ticker,
                "eligible_at": item.eligible_at,
            }
            for item in result.evidence_state.evidence_items
        ],
        "runtime_identity": runtime,
        "bound_runtime_identity": result.evidence_state.data_runtime_identity,
        "context_pack_sha256": result.context_pack.context_pack_sha256,
        "assessment_context_pack_sha256": result.assessment.context_pack_sha256,
        "plan_context_pack_sha256": result.validated_claim_plan.context_pack_sha256,
        "assessment": result.assessment,
        "validated_claim_plan": result.validated_claim_plan,
        "answer_text": result.answer.text,
    }


def _v1_metrics(run: dict[str, Any], case: Any) -> dict[str, float | int]:
    """Derive mechanical metrics from the ACTUAL V1 run artifacts."""
    from datetime import datetime as _datetime, timezone as _timezone

    cutoff_at = _datetime.fromisoformat(case.cutoff.replace("Z", "+00:00")).replace(
        tzinfo=_timezone.utc
    )
    violations = 0
    if run.get("retrieval_cutoff") != case.cutoff:
        violations += 1
    for item in run["evidence_items"]:
        if item.get("ticker") != case.ticker:
            violations += 1
        if item.get("eligible_at") is not None and item["eligible_at"] > cutoff_at:
            violations += 1

    citations, _claim_markers = extract_answer_markers(run["answer_text"])
    permitted = set(run["validated_claim_plan"].permitted_evidence_ids)
    resolved = sum(1 for citation in citations if citation in permitted)

    bound = 1 if run["bound_runtime_identity"] == run["runtime_identity"] else 0
    # ContextPack identity requires EXACT equality across the persisted pack,
    # the normalized assessment, and the validated claim plan (hex64 alone is
    # not sufficient).
    context_hashes = (
        run["context_pack_sha256"],
        run["assessment_context_pack_sha256"],
        run["plan_context_pack_sha256"],
    )
    context_ok = (
        1
        if len(set(context_hashes)) == 1
        and _HEX64_RE.fullmatch(context_hashes[0] or "")
        else 0
    )

    hypothesis_by_id = {
        hypothesis.hypothesis_id: hypothesis
        for hypothesis in run["assessment"].normalized_hypotheses
    }
    lineage_total = 0
    lineage_ok = 0
    for claim in run["validated_claim_plan"].claims:
        if claim.source_hypothesis_id and claim.support_evidence_ids:
            lineage_total += 1
            hypothesis = hypothesis_by_id.get(claim.source_hypothesis_id)
            if hypothesis is not None and set(claim.support_evidence_ids) <= set(
                hypothesis.supporting_evidence_ids
            ):
                lineage_ok += 1

    leaked = 1 if _SECRET_RE.search(_serialize(run)) else 0
    return {
        "ticker_cutoff_violations": violations,
        "citation_resolution": resolved / len(citations) if citations else 1.0,
        "runtime_identity_binding": bound,
        "context_pack_identity": context_ok,
        "claim_lineage": lineage_ok / lineage_total if lineage_total else 1.0,
        "secret_leakage": leaked,
    }


# --- sealed legacy reader side ------------------------------------------------

def legacy_fixture_run(case: Any, *, db_path: Path) -> dict[str, Any]:
    """Build a deterministic legacy MCJ fixture run and read it through the
    sealed ``baseline.adapter.read_legacy_run_artifacts`` reader."""
    import sqlite3

    from catalyst_agents.trace.schema import init_trace_db
    from catalyst_eval.baseline.adapter import read_legacy_run_artifacts

    evidence_id = f"chunk:{case.case_id}"
    run_id = f"legacy:{case.case_id}"
    trace_id = f"trace:{case.case_id}"
    now = "2026-01-15T21:00:00Z"
    conn = sqlite3.connect(db_path)
    init_trace_db(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO agent_runs
            (run_id, trace_id, ticker, trade_date, status, queued_at, started_at,
             ended_at, total_latency_ms, total_cost_usd, model_id_per_role,
             config, error_type, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, trace_id, case.ticker, case.session_date, "SUCCEEDED",
            now, now, now, 100, 0.0, None, "legacy-mcj", None, None,
        ),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO trace_events
            (run_id, trace_id, event_seq, node, started_at, ended_at, latency_ms,
             model_id, input_tokens, output_tokens, cost_usd, decision,
             error_type, error_message, status_before, status_after)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, trace_id, 1, "judge", now, now, 50, "model-legacy", 10, 10,
            0.0,
            json.dumps(
                {
                    "citations": [evidence_id],
                    "evidence_ids": [evidence_id],
                    "ticker": case.ticker,
                    "cutoff": case.cutoff,
                    "causes_count": 1,
                }
            ),
            None, None, "RUNNING", "SUFFICIENT",
        ),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO node_artifacts
            (run_id, event_seq, node, artifact_type, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, 1, "judge", "graded_evidence",
            json.dumps(
                [
                    {
                        "chunk_id": evidence_id,
                        "ticker": case.ticker,
                        "reference_date": case.session_date,
                        "rank": 1,
                    }
                ]
            ),
            now,
        ),
    )
    conn.commit()
    conn.close()
    return read_legacy_run_artifacts(db_path, run_id)


def _legacy_metrics(adapter_result: dict[str, Any], case: Any) -> dict[str, float | int]:
    """Derive mechanical metrics from the sealed reader's ACTUAL output."""
    agent = adapter_result.get("agent_run") or {}
    violations = 0
    if agent.get("ticker") != case.ticker:
        violations += 1
    decision: dict[str, Any] = {}
    for event in adapter_result.get("trace_events") or []:
        if isinstance(event.get("decision"), dict):
            decision = event["decision"]
    if decision.get("ticker") != case.ticker or decision.get("cutoff") != case.cutoff:
        violations += 1
    evidence_ids = set(decision.get("evidence_ids") or [])
    citations = set(decision.get("citations") or [])
    resolved = len(citations & evidence_ids)
    # Runtime identity binding: the sealed reader's actual agent_run row must
    # be bound to a trace identity and a claimed runtime (started_at/status).
    bound = 1 if agent.get("trace_id") and agent.get("started_at") and agent.get("status") else 0
    artifact_chunks: set[str] = set()
    for artifact in adapter_result.get("node_artifacts") or []:
        payload = artifact.get("payload")
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and item.get("chunk_id"):
                    artifact_chunks.add(item["chunk_id"])
    lineage_total = len(evidence_ids)
    lineage_ok = sum(1 for eid in evidence_ids if eid in artifact_chunks)
    leaked = 1 if _SECRET_RE.search(_serialize(adapter_result)) else 0
    return {
        "ticker_cutoff_violations": violations,
        "citation_resolution": resolved / len(citations) if citations else 1.0,
        "runtime_identity_binding": bound,
        "context_pack_identity": 1.0,  # legacy reader has no ContextPack; N/A
        "claim_lineage": lineage_ok / lineage_total if lineage_total else 1.0,
        "secret_leakage": leaked,
    }


def _aggregate_metrics(metric_list: list[dict[str, float | int]], count: int) -> dict[str, float | int]:
    return {
        "ticker_cutoff_violations": sum(
            int(item["ticker_cutoff_violations"]) for item in metric_list
        ),
        "citation_resolution": (
            sum(float(item["citation_resolution"]) for item in metric_list) / count
            if count
            else 1.0
        ),
        "runtime_identity_binding": (
            sum(float(item["runtime_identity_binding"]) for item in metric_list) / count
            if count
            else 1.0
        ),
        "context_pack_identity": (
            sum(float(item["context_pack_identity"]) for item in metric_list) / count
            if count
            else 1.0
        ),
        "claim_lineage": (
            sum(float(item["claim_lineage"]) for item in metric_list) / count
            if count
            else 1.0
        ),
        "secret_leakage": sum(int(item["secret_leakage"]) for item in metric_list),
    }


def semantic_ontology_regression_gate(
    *,
    repo_root: Path,
    golden_dir: Path,
    out_path: Path,
    v1_runner: Any = None,
) -> dict[str, Any]:
    """Gate B: real V1 fixture path + sealed legacy reader, derived metrics.

    Builds the approved 10-case T4 pack through repo case-pack code (never
    from /root-bound evidence refs), runs the REAL M5 V1 semantic path with
    deterministic fake providers, reads deterministic legacy fixture runs
    through the sealed ``baseline.adapter`` reader, and derives mechanical
    parity metrics from the ACTUAL outputs. The gate fails when the real V1
    path breaks citation resolution, ContextPack identity, runtime identity,
    claim lineage, ticker/cutoff, or secret leakage. No quality or comparable
    claim is made (comparability_declared=false).
    """
    del repo_root
    cases = build_smoke_case_pack(Path(golden_dir))
    pack_id = compute_case_pack_id(cases)
    ordered_ids = tuple(case.case_id for case in cases)
    if pack_id != APPROVED_T4_CASE_PACK_ID:
        raise GateFailure(
            f"T4 case pack ID mismatch: {pack_id} != {APPROVED_T4_CASE_PACK_ID}"
        )
    if len(cases) != APPROVED_T4_CASE_COUNT:
        raise GateFailure(f"T4 case pack count mismatch: {len(cases)}")
    if ordered_ids != APPROVED_T4_ORDERED_CASE_IDS:
        raise GateFailure("T4 case pack ordered IDs do not match the approved pack")

    runner = v1_runner or v1_fixture_run
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "legacy_fixture.db"
        legacy_metrics: list[dict[str, float | int]] = []
        v1_metrics: list[dict[str, float | int]] = []
        for case in cases:
            legacy = legacy_fixture_run(case, db_path=db_path)
            legacy_metrics.append(_legacy_metrics(legacy, case))
            v1_metrics.append(_v1_metrics(runner(case), case))

    legacy_aggregate = _aggregate_metrics(legacy_metrics, len(cases))
    v1_aggregate = _aggregate_metrics(v1_metrics, len(cases))

    for metric in _GATE_B_METRICS:
        if legacy_aggregate[metric] != v1_aggregate[metric]:
            raise GateFailure(f"metric parity failure on {metric}: legacy/v1 diverge")

    # Invariants derived from the real V1 path's actual artifacts.
    if v1_aggregate["ticker_cutoff_violations"] != 0:
        raise GateFailure("ticker/cutoff violations in V1 path")
    if v1_aggregate["citation_resolution"] != 1.0:
        raise GateFailure("citation resolution failed in V1 path")
    if v1_aggregate["runtime_identity_binding"] != 1.0:
        raise GateFailure("runtime identity binding failed in V1 path")
    if v1_aggregate["context_pack_identity"] != 1.0:
        raise GateFailure("context pack identity failed in V1 path")
    if v1_aggregate["claim_lineage"] != 1.0:
        raise GateFailure("claim lineage failed in V1 path")
    if v1_aggregate["secret_leakage"] != 0:
        raise GateFailure("secret leakage detected in V1 path")

    payload = {
        "schema_version": GATE_B_SCHEMA,
        "case_pack": {
            "approved_case_pack_id": pack_id,
            "case_count": len(cases),
            "ordered_case_ids": list(ordered_ids),
            "matched": True,
        },
        "metric_parity": {key: v1_aggregate[key] for key in _GATE_B_METRICS},
        "comparability_declared": False,
        "exit_status": 0,
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


__all__ = [
    "APPROVED_T4_CASE_PACK_ID",
    "FOUR_ARM_TOKEN",
    "GATE_A_SCHEMA",
    "GATE_B_SCHEMA",
    "GateFailure",
    "Q002_PROMOTED_ENV_REASON",
    "SEALED_BASELINE_REPORT",
    "SEALED_BASELINE_SHA256",
    "SEALED_BASELINE_SCHEMA",
    "SEALED_BASELINE_TAG",
    "USER_SMOKE_TOKEN",
    "legacy_fixture_run",
    "sealed_baseline_integrity_gate",
    "semantic_ontology_regression_gate",
    "v1_fixture_run",
]
