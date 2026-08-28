"""Wave 3 production attribution user-smoke runner (importable core).

The smoke exercises the real user-visible path:

    loopback HTTP -> Catalyst app -> live run -> runtime dependency loader
    -> attribution graph -> retrieval/tool execution -> DeepSeek answer
    -> exported trace / RunAssuranceRecord / immutable evidence

Production entry point: ``packages/eval/scripts/run_post_import_user_smoke.py``.
This module contains the testable orchestration and fail-closed validation
shared by that CLI.  Direct graph-internal invocation is allowed only in unit
tests, never as the production smoke execution path.

Gates (fail closed before provider use):

- exact clean Git runtime identity resolved from disk;
- frozen DB identity/counts and LanceDB/index manifest identities validated;
- T4 evidence validates against the selected case pack/runtime identity;
- a successful full Wave 2 evidence directory exists with a code-produced
  ``FOUR_ARM_E2E_OK`` (never CLI-supplied);
- CUDA + production pinned embedding/reranker boundaries satisfied;
- app dependencies are wired with the explicit authoritative index manifest
  path;
- provider/model validation succeeds without exposing credentials.

The CLI never accepts a caller-supplied success token, fake validation
boolean, raw identity strings, or an API key argument.  ``USER_SMOKE_OK`` is
written only when every gate, the three expected statuses, trace/assurance
checks, evidence identity checks, cost gate, failure-path checks, and secret
scan pass; it is never written for partial/mocked/degraded/unknown-cost runs,
for ``--limit``-style partial execution, or on expectation mismatch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from catalyst_data.corpus.streaming_publication import served_chunks_relation
from catalyst_data.retrieval.result import SEARCHABLE_STATUSES
from catalyst_agents.runtime.assurance.record import RunAssuranceRecord
from catalyst_agents.trace.exporter import export_run

from catalyst_eval.post_import.case_pack import CasePackCase
from catalyst_eval.post_import.four_arm import (
    EmbeddingBoundary,
    RunnerValidationError,
    validate_embedding_boundary,
)
from catalyst_eval.post_import.index_identity import ResolvedRuntimeIdentity
from catalyst_eval.post_import.t4_evidence import (
    ValidatedT4Evidence,
    validate_t4_evidence,
)

USER_SMOKE_TOKEN = "USER_SMOKE_OK"
WAVE_TOKEN_FILENAME = "WAVE_TOKEN.txt"
WAVE2_FOUR_ARM_TOKEN = "FOUR_ARM_E2E_OK"
META_SCHEMA_VERSION = "post_import_user_smoke_meta_v1"
CASE_RESULTS_SCHEMA = "post_import_user_smoke_case_results_v1"
FAILURE_PATHS_SCHEMA = "post_import_user_smoke_failure_paths_v1"

# Stable approved Wave 3 selection (from the approved golden/case-pack sources).
USER_SMOKE_CASE_IDS = ("g006", "g007", "h004")
USER_SMOKE_EXPECTED_CLASSES = {
    "g006": "SUFFICIENT",
    "g007": "PARTIAL",
    "h004": "ABSTAIN",
}

# Path-specific required (node, artifact_type) matrix (AMEND-5.1).
# Base path applies to every case; ABSTAIN and judge/validator paths add more.
REQUIRED_NODE_ARTIFACT_MATRIX: dict[str, frozenset[tuple[str, str]]] = {
    "base": frozenset({
        ("context_builder", "context_artifact"),
        ("context_builder", "state_snapshot"),
        ("miner", "retrieved_chunks"),
        ("miner", "reranked_chunks"),
        ("miner", "arm_b_evidence"),
        ("miner", "state_snapshot"),
        ("critic", "graded_evidence"),
        ("critic", "all_graded_chunks"),
        ("critic", "critic_decision"),
        ("critic", "raw_llm_response"),
        ("critic", "state_snapshot"),
        ("finalizer", "state_snapshot"),
    }),
    "abstain": frozenset({
        ("insufficient_handler", "state_snapshot"),
    }),
    "judge_validator": frozenset({
        ("judge", "judge_evidence"),
        ("judge", "raw_llm_response"),
        ("judge", "state_snapshot"),
        # AMEND-7: judge_causes/judge_summary are owned by the Validator node
        # so the persisted public artifacts are projected from the post-filter
        # state. The same artifact types remain required on every
        # judge/validator path; this is not a gate weakening.
        ("validator", "judge_causes"),
        ("validator", "judge_summary"),
        ("validator", "validator_decision"),
        ("validator", "raw_llm_response"),
        ("validator", "state_snapshot"),
    }),
}

# Legacy type-set view (union of all matrix pairs) kept for inventory export.
REQUIRED_NODE_ARTIFACT_TYPES = frozenset(
    artifact_type
    for pairs in REQUIRED_NODE_ARTIFACT_MATRIX.values()
    for _, artifact_type in pairs
)

COST_CEILING_USD = 5.0
POLL_DEFAULT_TIMEOUT_SECONDS = 300.0
POLL_DEFAULT_INTERVAL_SECONDS = 0.5
DEFAULT_PROVIDER = "deepseek"
DEFAULT_MODEL_ID = "deepseek-v4-flash"

# DeepSeek retired deepseek-chat/deepseek-reasoner on 2026-07-24 (manager
# decision); the runner must fail closed rather than silently fall back.
RETIRED_MODEL_ALIASES = ("deepseek-chat", "deepseek-reasoner")

EXPECTED_WAVE2_CASE_COUNT = 10

_TERMINAL_STATUSES = {
    "SUCCEEDED", "SUFFICIENT", "PARTIAL", "ABSTAIN",
    "FAILED_SYSTEM", "SYSTEM_ERROR", "FAILED_REQUEST", "CANCELLED",
    # M6 app lifecycle terminal statuses (RunDTO.lifecycle_status).
    "COMPLETED", "FAILED",
}
_FAILED_TRANSPORT = {"FAILED_SYSTEM", "SYSTEM_ERROR", "FAILED_REQUEST", "CANCELLED", "FAILED"}

_SECRET_KEYS = frozenset({
    "authorization", "api_key", "x-api-key", "cookie", "set-cookie",
    "proxy-authorization", "deepseek_api_key", "openai_api_key",
    "anthropic_api_key", "aihubmix_api_key", "siliconflow_api_key",
    "gemini_api_key", "glm_api_key",
})
_SECRET_VALUE_RE = re.compile(
    r"(?i)(sk-[a-z0-9_-]{8,}|[a-z0-9_-]+_api_key\s*[:=]\s*\S+)"
)
_SECRET_SCAN_PATTERNS = (
    re.compile(r"(?i)sk-[a-z0-9_-]{8,}"),
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(deepseek|openai|anthropic|aihubmix|siliconflow|gemini|glm|zai)_api_key\s*[:=]\s*\S+"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    _atomic_write_text(path, text)


def _open_frozen_db_readonly(path: Path) -> sqlite3.Connection:
    """Open the protected frozen DB with an immutable read-only URI.

    The runner never opens the source protected DB writable; all runtime
    writes go to the writable derivative DB provided by the operator.
    """
    uri = f"{Path(path).resolve().as_uri()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _model_pricing_rate(model_id: str, direction: str) -> float | None:
    """Read the runtime pricing lock without exposing credentials.

    Returns None when the model has no lock (the cost gate fails closed later).
    """
    try:
        from catalyst_agents.cost_tracker import MODEL_PRICING

        entry = MODEL_PRICING.get(model_id)
        if entry is None:
            return None
        return float(entry.get(direction))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Stable case selection
# ---------------------------------------------------------------------------


def normalize_expected_class(raw_status: str) -> str:
    """Normalize the golden status at the documented user-smoke boundary."""
    if raw_status == "INSUFFICIENT":
        return "ABSTAIN"
    return raw_status


@dataclass(frozen=True)
class UserSmokeCase:
    case: CasePackCase
    expected_class: str
    raw_expected_status: str


def select_user_smoke_cases(cases: list[CasePackCase]) -> list[UserSmokeCase]:
    """Select exactly the three approved cases by stable ID.

    Preserves the h004 query_override from the approved source; never hand
    copies or silently mutates golden evidence fields.
    """
    by_id: dict[str, CasePackCase] = {}
    for case in cases:
        if case.case_id in by_id:
            raise ValueError(f"duplicate case_id in case pack: {case.case_id}")
        by_id[case.case_id] = case
    missing = [case_id for case_id in USER_SMOKE_CASE_IDS if case_id not in by_id]
    if missing:
        raise ValueError(f"user-smoke cases missing from case pack: {', '.join(missing)}")
    selected: list[UserSmokeCase] = []
    for case_id in USER_SMOKE_CASE_IDS:
        case = by_id[case_id]
        raw = str(case.golden.get("expected_status") or "")
        expected_class = USER_SMOKE_EXPECTED_CLASSES[case_id]
        if raw and normalize_expected_class(raw) != expected_class:
            raise ValueError(
                f"golden expected_status {raw!r} for {case_id} does not match approved class {expected_class}"
            )
        selected.append(UserSmokeCase(
            case=case,
            expected_class=expected_class,
            raw_expected_status=raw,
        ))
    return selected


# ---------------------------------------------------------------------------
# Wave 2 evidence validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidatedWave2Evidence:
    wave2_dir: Path
    four_arm_token: str
    four_arm_meta: dict[str, Any]
    t4_evidence: ValidatedT4Evidence


def _effect_validity_from_artifact(artifact) -> list[str]:
    """Re-run AMEND-5 effect-validity invariants against a loaded arm artifact."""
    problems: list[str] = []
    effect = artifact.payload.get("effect_metrics") or {}
    arms = artifact.arms
    for name, count_key in (
        ("fts5", "lexical_count"),
        ("dense", "dense_count"),
        ("hybrid", "hybrid_count"),
        ("reranked", "reranked_count"),
    ):
        arm = arms.get(name) or {}
        if arm.get("status") != "ok":
            problems.append(f"{name} arm failed")
        results = arm.get("results") or []
        if not results:
            problems.append(f"{name} arm ok with zero results")
        if effect.get(count_key) == 0:
            problems.append(f"effect_metrics.{count_key}=0")
    if effect.get("hybrid_lexical_contribution", 0) == 0:
        problems.append("hybrid lexical contribution is zero")
    if effect.get("hybrid_dense_contribution", 0) == 0:
        problems.append("hybrid dense contribution is zero")
    if effect.get("reranker_input_count", 0) == 0:
        problems.append("reranker input count is zero")
    if effect.get("reranker_output_count", 0) == 0:
        problems.append("reranker output count is zero")
    provenance = effect.get("reranker_provenance") or []
    if not provenance:
        problems.append("reranker provenance incomplete")
    # production_pinned / successful rerank serve: every hit needs finite score+rank.
    from catalyst_data.retrieval.artifacts import is_finite_number

    reranked_arm = arms.get("reranked") or {}
    reranked_results = reranked_arm.get("results") or []
    if (
        reranked_arm.get("mode_served") == "reranked"
        and reranked_arm.get("status") == "ok"
        and reranked_results
    ):
        for position, result in enumerate(reranked_results, start=1):
            if not is_finite_number(result.get("reranker_score")):
                problems.append(
                    f"reranked result rank={position} missing finite reranker_score"
                )
            rank = result.get("reranker_rank")
            if type(rank) is not int or type(rank) is bool or rank < 1:
                problems.append(
                    f"reranked result rank={position} missing valid reranker_rank"
                )
            elif rank != position:
                problems.append(
                    f"reranked result rank={position} reranker_rank not contiguous"
                )
        for position, entry in enumerate(provenance, start=1):
            if not isinstance(entry, dict):
                problems.append(f"provenance entry {position} invalid")
                continue
            if not is_finite_number(entry.get("reranker_score")):
                problems.append(
                    f"provenance rank={position} missing finite reranker_score"
                )
            rank = entry.get("reranker_rank")
            if type(rank) is not int or type(rank) is bool or rank < 1:
                problems.append(f"provenance rank={position} missing reranker_rank")
    return problems


def validate_wave2_evidence(
    *,
    wave2_dir: Path,
    t4_evidence_dir: Path,
    current_case_pack: list[CasePackCase] | None,
    resolved: ResolvedRuntimeIdentity | None,
) -> ValidatedWave2Evidence:
    """Fail-closed validation of the full Wave 2 evidence directory.

    The success token must exist with the exact code-produced content
    ``FOUR_ARM_E2E_OK``; it is never supplied by CLI or user.  The four-arm
    meta must be identity-bound to the resolved runtime and describe a full
    (non-limited) production-pinned 10-case run.

    AMEND-5.1: every arm artifact and union pool is reloaded; schema version,
    artifact_id, pool.source_artifact_id, exact approved case IDs, and
    effect_metrics invariants are verified.  Legacy pre-effect_metrics
    evidence directories (e.g. four_arm_full_wave23_final3) fail closed.
    """
    from catalyst_data.retrieval.artifacts import (
        ARM_ARTIFACT_SCHEMA_VERSION,
        ArtifactValidationError,
        load_arm_artifact,
    )
    from catalyst_data.retrieval.pool import load_union_pool
    from catalyst_eval.post_import.four_arm import (
        validate_temporal_identity_validation,
    )

    wave2_dir = Path(wave2_dir).resolve()
    if not wave2_dir.is_dir():
        raise ValueError(f"wave2 evidence directory missing: {wave2_dir}")

    token_path = wave2_dir / WAVE_TOKEN_FILENAME
    if not token_path.is_file():
        raise ValueError(f"{WAVE_TOKEN_FILENAME} missing in wave2 evidence directory")
    token = token_path.read_text(encoding="utf-8").strip()
    if token != WAVE2_FOUR_ARM_TOKEN:
        raise ValueError(f"{WAVE_TOKEN_FILENAME} content must be {WAVE2_FOUR_ARM_TOKEN}")

    meta_path = wave2_dir / "meta.json"
    if not meta_path.is_file():
        raise ValueError("wave2 meta.json missing")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise ValueError("wave2 meta.json must be an object")

    if meta.get("embedding_mode") != "production_pinned":
        raise ValueError("wave2 run must be production_pinned")
    case_count = meta.get("case_count")
    full_case_count = meta.get("full_case_count")
    if case_count != EXPECTED_WAVE2_CASE_COUNT or full_case_count != EXPECTED_WAVE2_CASE_COUNT:
        raise ValueError(
            f"wave2 run must be a full {EXPECTED_WAVE2_CASE_COUNT}-case run"
        )

    if resolved is not None:
        # The four-arm run meta binds runtime, index, snapshot, and corpus
        # identities.  The frozen DB SHA is bound through the T4 evidence
        # (validated above) and the resolved runtime identity, and is not a
        # four-arm meta field by design.
        comparisons = (
            ("runtime_git_head", meta.get("runtime_git_head"), resolved.git_head),
            ("index_manifest_id", meta.get("index_manifest_id"), resolved.index_manifest_id),
            ("snapshot_id", meta.get("snapshot_id"), resolved.snapshot_id),
            ("corpus_manifest_id", meta.get("corpus_manifest_id"), resolved.corpus_manifest_id),
        )
        for label, actual, expected in comparisons:
            if actual != expected:
                raise ValueError(f"wave2 meta.{label} does not match resolved runtime identity")

    arms_dir = wave2_dir / "arms"
    pool_dir = wave2_dir / "pool"
    if not arms_dir.is_dir() or not pool_dir.is_dir():
        raise ValueError("wave2 arms/ or pool/ directory missing")
    arm_files = sorted(arms_dir.glob("*.json"))
    pool_files = sorted(pool_dir.glob("*.json"))
    if len(arm_files) != EXPECTED_WAVE2_CASE_COUNT:
        raise ValueError(
            f"wave2 arms must contain {EXPECTED_WAVE2_CASE_COUNT} artifacts"
        )
    if len(pool_files) != EXPECTED_WAVE2_CASE_COUNT:
        raise ValueError(
            f"wave2 pool must contain {EXPECTED_WAVE2_CASE_COUNT} artifacts"
        )

    t4_evidence = validate_t4_evidence(
        evidence_dir=t4_evidence_dir,
        current_case_pack=current_case_pack,
        resolved=resolved,
    )

    if current_case_pack is not None:
        approved_ids = [case.case_id for case in current_case_pack]
        temporal_cases = list(current_case_pack)
    else:
        # Load the pack bound into the validated T4 evidence directory.
        from catalyst_eval.post_import.case_pack import load_case_pack

        pack_path = Path(t4_evidence.evidence_dir) / "case_pack.jsonl"
        temporal_cases = list(load_case_pack(pack_path))
        approved_ids = [case.case_id for case in temporal_cases]
    if len(approved_ids) != EXPECTED_WAVE2_CASE_COUNT:
        raise ValueError(
            f"approved case pack must contain {EXPECTED_WAVE2_CASE_COUNT} cases, "
            f"got {len(approved_ids)}"
        )
    expected_case_ids = set(approved_ids)
    if len(expected_case_ids) != len(approved_ids):
        raise ValueError("approved case pack contains duplicate case IDs")

    temporal_problems = validate_temporal_identity_validation(
        meta.get("temporal_identity_validation"), temporal_cases,
    )
    if temporal_problems:
        raise ValueError(
            "wave2 meta temporal_identity_validation failed: "
            + "; ".join(temporal_problems)
        )

    # Case pack by id for semantic binding (query/ticker/cutoff).
    cases_by_id: dict[str, CasePackCase] = {}
    if current_case_pack is not None:
        for case in current_case_pack:
            cases_by_id[case.case_id] = case
    else:
        from catalyst_eval.post_import.case_pack import load_case_pack

        pack_path = Path(t4_evidence.evidence_dir) / "case_pack.jsonl"
        for case in load_case_pack(pack_path):
            cases_by_id[case.case_id] = case

    # Wave 2 meta run_id + corpus/index identities for arm binding.
    meta_run_id = meta.get("run_id")
    if not isinstance(meta_run_id, str) or not meta_run_id:
        raise ValueError("wave2 meta.run_id is required for arm binding")
    meta_corpus_id = meta.get("corpus_manifest_id")
    meta_index_id = meta.get("index_manifest_id")
    if resolved is not None:
        if meta_corpus_id != resolved.corpus_manifest_id:
            raise ValueError("wave2 meta.corpus_manifest_id does not match resolved runtime")
        if meta_index_id != resolved.index_manifest_id:
            raise ValueError("wave2 meta.index_manifest_id does not match resolved runtime")

    loaded_arms: dict[str, Any] = {}
    for path in arm_files:
        try:
            artifact = load_arm_artifact(path)
        except ArtifactValidationError as exc:
            raise ValueError(
                f"wave2 arm artifact {path.name} failed reload validation: {exc}"
            ) from exc
        if artifact.schema_version != ARM_ARTIFACT_SCHEMA_VERSION:
            raise ValueError(
                f"wave2 arm artifact {path.name} schema_version "
                f"{artifact.schema_version!r} != {ARM_ARTIFACT_SCHEMA_VERSION}"
            )
        case_id = artifact.payload.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"wave2 arm artifact {path.name} missing case_id")
        if case_id in loaded_arms:
            raise ValueError(f"wave2 arm artifacts contain duplicate case_id {case_id}")
        if artifact.artifact_id != artifact.payload.get("artifact_id"):
            raise ValueError(f"wave2 arm artifact {path.name} artifact_id mismatch")
        # AMEND-5.2: bind each arm to approved case + Wave 2 meta identities.
        case = cases_by_id.get(case_id)
        if case is None:
            raise ValueError(f"wave2 arm artifact {case_id} not in approved case pack")
        expected_query_sha = hashlib.sha256(case.query.encode("utf-8")).hexdigest()
        actual_query_sha = artifact.payload.get("query_sha256")
        if actual_query_sha != expected_query_sha:
            raise ValueError(
                f"wave2 arm artifact {case_id} query_sha256 does not match approved case query hash"
            )
        if artifact.payload.get("cutoff_ts") != case.cutoff:
            raise ValueError(
                f"wave2 arm artifact {case_id} cutoff_ts does not match approved case cutoff"
            )
        filters = artifact.payload.get("filters") or {}
        if filters.get("ticker") != case.ticker:
            raise ValueError(
                f"wave2 arm artifact {case_id} filters.ticker does not match approved case ticker"
            )
        if artifact.payload.get("run_id") != meta_run_id:
            raise ValueError(
                f"wave2 arm artifact {case_id} run_id does not match wave2 meta.run_id"
            )
        if filters.get("corpus_manifest_id") != meta_corpus_id:
            raise ValueError(
                f"wave2 arm artifact {case_id} filters.corpus_manifest_id does not match meta/resolved"
            )
        if filters.get("index_manifest_id") != meta_index_id:
            raise ValueError(
                f"wave2 arm artifact {case_id} filters.index_manifest_id does not match meta/resolved"
            )
        problems = _effect_validity_from_artifact(artifact)
        if problems:
            raise ValueError(
                f"wave2 arm artifact {case_id} effect-validity failed: {'; '.join(problems)}"
            )
        loaded_arms[case_id] = artifact

    arm_case_ids = set(loaded_arms)
    if arm_case_ids != expected_case_ids:
        missing = sorted(expected_case_ids - arm_case_ids)
        extra = sorted(arm_case_ids - expected_case_ids)
        raise ValueError(
            f"wave2 arm case IDs mismatch approved pack "
            f"(missing={missing}, extra={extra})"
        )

    loaded_pools: dict[str, Any] = {}
    for path in pool_files:
        try:
            pool = load_union_pool(path)
        except Exception as exc:
            raise ValueError(
                f"wave2 pool artifact {path.name} failed reload validation: {exc}"
            ) from exc
        case_id = pool.case_id
        if case_id in loaded_pools:
            raise ValueError(f"wave2 pool artifacts contain duplicate case_id {case_id}")
        arm = loaded_arms.get(case_id)
        if arm is None:
            raise ValueError(f"wave2 pool {case_id} has no matching arm artifact")
        if pool.source_artifact_id != arm.artifact_id:
            raise ValueError(
                f"wave2 pool {case_id} source_artifact_id does not match arm artifact_id"
            )
        loaded_pools[case_id] = pool

    pool_case_ids = set(loaded_pools)
    if pool_case_ids != expected_case_ids:
        missing = sorted(expected_case_ids - pool_case_ids)
        extra = sorted(pool_case_ids - expected_case_ids)
        raise ValueError(
            f"wave2 pool case IDs mismatch approved pack "
            f"(missing={missing}, extra={extra})"
        )

    return ValidatedWave2Evidence(
        wave2_dir=wave2_dir,
        four_arm_token=token,
        four_arm_meta=meta,
        t4_evidence=t4_evidence,
    )


# ---------------------------------------------------------------------------
# Evidence/citation identity
# ---------------------------------------------------------------------------


def _chunk_served_for_case(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    manifest_id: str,
    ticker: str,
    cutoff: str,
) -> tuple[str | None, str | None]:
    """Return (available_at, source_class) when the chunk is served for the case."""
    relation = served_chunks_relation(conn)
    status_placeholders = ", ".join("?" for _ in SEARCHABLE_STATUSES)
    row = conn.execute(
        f"""SELECT c.available_at, c.source_class
            FROM {relation} c
            WHERE c.chunk_id = ?
              AND c.manifest_id = ?
              AND c.status IN ({status_placeholders})
              AND c.eligibility = 'eligible'
              AND c.available_at <= ?
              AND EXISTS (
                SELECT 1 FROM json_each(c.ticker_associations) je
                WHERE je.value = ?
              )""",
        (chunk_id, manifest_id, *SEARCHABLE_STATUSES, cutoff, ticker),
    ).fetchone()
    if row is None:
        return None, None
    return str(row[0]), str(row[1])


def _validate_citations(
    frozen_db_path: Path,
    *,
    case: CasePackCase,
    cited_ids: list[str],
    manifest_id: str,
) -> tuple[bool, str]:
    """Every citation must resolve to a served frozen-corpus chunk within cutoff."""
    if not cited_ids:
        return True, "no citations to validate"
    unique = sorted(set(cited_ids))
    conn = _open_frozen_db_readonly(frozen_db_path)
    try:
        unresolvable: list[str] = []
        lookahead: list[str] = []
        for chunk_id in unique:
            available_at, _ = _chunk_served_for_case(
                conn,
                chunk_id=chunk_id,
                manifest_id=manifest_id,
                ticker=case.ticker,
                cutoff=case.cutoff,
            )
            if available_at is None:
                unresolvable.append(chunk_id)
            elif available_at > case.cutoff:
                lookahead.append(chunk_id)
    finally:
        conn.close()
    problems: list[str] = []
    if unresolvable:
        problems.append(f"unresolvable citations: {sorted(unresolvable)}")
    if lookahead:
        problems.append(f"look-ahead citations after cutoff: {sorted(lookahead)}")
    return (not problems), "; ".join(problems) if problems else "citations resolve"


def _cited_ids_from_workspace(workspace: dict[str, Any]) -> list[str]:
    cited: list[str] = []
    result = workspace.get("result")
    if isinstance(result, dict):
        for cause in result.get("causes") or []:
            if not isinstance(cause, dict):
                continue
            cited.extend(cause.get("evidence_ids") or [])
    # M6 V1.1 workspace projection: claims carry citation evidence ids.
    for claim in workspace.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        cited.extend(claim.get("citation_evidence_ids") or [])
    return cited


def _aggregate_cost(events: list[dict[str, Any]]) -> tuple[float | None, bool]:
    total = 0.0
    for event in events:
        cost = event.get("cost_usd")
        if cost is None:
            return None, False
        total += float(cost or 0.0)
    if not events:
        return None, False
    return total, True


def _provider_calls_in_events(events: list[dict[str, Any]]) -> int:
    return sum(1 for event in events if event.get("model_id"))


def _run_latency_ms(workspace: dict[str, Any], events: list[dict[str, Any]]) -> int | None:
    runtime_ms = workspace.get("runtime_ms")
    if isinstance(runtime_ms, int):
        return runtime_ms
    latencies = [event.get("latency_ms") for event in events if isinstance(event.get("latency_ms"), int)]
    return sum(latencies) if latencies else None


# ---------------------------------------------------------------------------
# Redaction and secret scan
# ---------------------------------------------------------------------------


def _redact_payload(payload: Any) -> Any:
    """Recursively redact secret-shaped keys and values (never echoes keys)."""
    if isinstance(payload, dict):
        return {
            key: "[REDACTED]" if str(key).lower() in _SECRET_KEYS else _redact_payload(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_redact_payload(item) for item in payload]
    if isinstance(payload, str):
        if _SECRET_VALUE_RE.search(payload):
            return "[REDACTED]"
        return payload
    return payload


def _secret_scan(directory: Path, *, secret_values: tuple[str, ...] = ()) -> list[str]:
    """Scan evidence files for configured secret values or secret-shaped text."""
    violations: list[str] = []
    for path in Path(directory).rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for value in secret_values:
            if value and value in text:
                violations.append(f"{path.name}: configured secret value present")
        for pattern in _SECRET_SCAN_PATTERNS:
            found = False
            for match in pattern.finditer(text):
                if "[REDACTED]" in match.group(0):
                    continue
                violations.append(f"{path.name}: secret pattern {pattern.pattern!r}")
                found = True
                break
            if found:
                break
    return violations


# ---------------------------------------------------------------------------
# HTTP session seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    json: Any = None
    headers: dict[str, str] = field(default_factory=dict)


def _require_json_response(resp: HttpResponse, *, endpoint: str) -> dict[str, Any]:
    if resp.status_code != 200:
        raise RunnerValidationError(f"{endpoint} returned HTTP {resp.status_code}")
    payload = resp.json
    if not isinstance(payload, dict):
        raise RunnerValidationError(f"{endpoint} returned a non-object payload")
    return payload


def _require_ready_health(http: Any) -> dict[str, Any]:
    payload = _require_json_response(http.get("/api/health/runtime"), endpoint="health")
    if payload.get("status") != "ready":
        raise RunnerValidationError(
            f"runtime health not ready: {payload.get('status')} "
            f"{payload.get('errors') or []}"
        )
    return payload


def _validate_provider_http(
    http: Any,
    *,
    provider: str,
    model_id: str,
    base_url: str | None,
    credential_source: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "provider": provider,
        "model_id": model_id,
        "api_key": "",
        "credential_source": credential_source,
    }
    if base_url is not None:
        body["base_url"] = base_url
    payload = _require_json_response(
        http.post("/api/models/validate", json=body),
        endpoint="provider validation",
    )
    if payload.get("ok") is not True:
        raise RunnerValidationError(
            f"provider/model validation failed: {payload.get('status')} {payload.get('message') or ''}"
        )
    return payload


def _model_config(
    *,
    provider: str,
    model_id: str,
    base_url: str | None,
    credential_source: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "provider": provider,
        "model_id": model_id,
        "credential_source": credential_source,
    }
    if base_url is not None:
        body["base_url"] = base_url
    return body


# ---------------------------------------------------------------------------
# Failure paths (same public boundary, provider calls asserted zero)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailurePathResult:
    name: str
    ok: bool
    detail: str
    provider_calls: int


def _submit_then_poll_typed_failure(
    http: Any,
    *,
    body: dict[str, Any],
    expected_codes: set[str],
    poll_interval_seconds: float = 0.01,
    poll_timeout_seconds: float = 5.0,
) -> tuple[bool, str]:
    """Submit through the M6 admission route and wait for a typed run failure.

    The M6 app admits syntactically valid requests durably and rejects
    unsupported tickers/dates during execution (before any provider dispatch);
    the probe therefore polls the authoritative RunDTO for the typed failure
    code and asserts zero provider calls at the caller.
    """
    resp = http.post("/api/live-runs", json=body)
    payload = resp.json
    if resp.status_code != 200 or not isinstance(payload, dict):
        return False, f"submit unexpected HTTP {resp.status_code}"
    if payload.get("status") != "ACCEPTED":
        return False, f"expected ACCEPTED submit, got {payload.get('status')}"
    run_id = payload.get("run_id")
    if not run_id:
        return False, "submit missing run_id"
    deadline = time.monotonic() + poll_timeout_seconds
    while time.monotonic() < deadline:
        poll = http.get(f"/api/live-runs/{run_id}")
        data = poll.json
        if poll.status_code != 200 or not isinstance(data, dict):
            return False, f"poll unexpected HTTP {poll.status_code}"
        status = data.get("status") or data.get("lifecycle_status") or "UNKNOWN"
        if status in ("FAILED", "FAILED_SYSTEM", "SYSTEM_ERROR"):
            failure = data.get("failure") or {}
            code = failure.get("code") or failure.get("sub_reason")
            if code in expected_codes:
                return True, code
            return False, (
                f"expected failure code in {sorted(expected_codes)}, got {code!r}"
            )
        if status in ("COMPLETED", "SUFFICIENT", "PARTIAL", "ABSTAIN", "SUCCEEDED"):
            return False, f"run unexpectedly completed: {status}"
        time.sleep(poll_interval_seconds)
    return False, "poll timeout waiting for typed failure"


def _exercise_failure_paths(
    http: Any,
    *,
    provider: str,
    model_id: str,
    base_url: str | None,
    credential_source: str,
) -> list[FailurePathResult]:
    """Deterministic no-provider failure paths through the M6 public boundary."""
    results: list[FailurePathResult] = []
    model = _model_config(
        provider=provider,
        model_id=model_id,
        base_url=base_url,
        credential_source=credential_source,
    )

    # invalid ticker: admitted durably, then rejected before any provider call.
    ok, detail = _submit_then_poll_typed_failure(
        http,
        body={
            "ticker": "ZZZZ",
            "trade_date": "2025-07-24",
            "query": "Why did ZZZZ move?",
            "model": model,
            "config": "mcj_full",
        },
        expected_codes={"unsupported_ticker"},
    )
    results.append(FailurePathResult("invalid_ticker", ok, detail, provider_calls=0))

    # invalid/future date: admitted durably, then rejected before any provider call.
    ok, detail = _submit_then_poll_typed_failure(
        http,
        body={
            "ticker": "TSLA",
            "trade_date": "2099-01-01",
            "query": "Why did TSLA move?",
            "model": model,
            "config": "mcj_full",
        },
        expected_codes={
            "date_out_of_range",
            "missing_trading_day_context",
            "invalid_trade_date",
        },
    )
    results.append(FailurePathResult("invalid_trade_date", ok, detail, provider_calls=0))

    # provider credential absent (browser_key with no api_key -> zero network)
    resp = http.post("/api/models/validate", json={
        "provider": provider,
        "model_id": model_id,
        "api_key": "",
        "credential_source": "browser_key",
    })
    payload = resp.json
    ok = isinstance(payload, dict) and payload.get("status") == "auth_failed"
    results.append(FailurePathResult(
        "missing_provider_credential", ok, str(payload.get("message") or payload), provider_calls=0,
    ))

    # model validation failure (unknown provider -> invalid, zero network)
    resp = http.post("/api/models/validate", json={
        "provider": "unknown_provider",
        "model_id": model_id,
        "api_key": "placeholder-key",
        "credential_source": "browser_key",
    })
    payload = resp.json
    ok = isinstance(payload, dict) and payload.get("status") == "invalid"
    results.append(FailurePathResult(
        "model_validation_failure", ok, str(payload.get("message") or payload), provider_calls=0,
    ))
    return results


# ---------------------------------------------------------------------------
# Case result / summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserSmokeCaseResult:
    case_id: str
    run_id: str
    transport_status: str
    output_status: str | None
    expected_class: str
    raw_expected_status: str
    match: bool
    citations_ok: bool
    citation_detail: str
    latency_ms: int | None
    retry_count: int | None
    provider_model: str | None
    finish_reason: str | None
    error_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    provider_calls: int
    detail: str


@dataclass(frozen=True)
class UserSmokeSummary:
    run_id: str
    case_count: int
    token_written: bool
    completed_at: str
    meta_path: Path
    total_cost_usd: float | None
    cost_known: bool
    failure_path_results: list[FailurePathResult]


def _success_token_gate(
    *,
    selected: list[UserSmokeCase],
    results: list[UserSmokeCaseResult],
    assurance_records: list[RunAssuranceRecord | None],
    incomplete_evidence: list[str],
    failure_results: list[FailurePathResult],
    cost_known: bool,
    total_cost_usd: float,
    cost_ceiling_usd: float,
    degraded: bool,
    expected_model_id: str,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if len(results) != len(selected):
        reasons.append("case count mismatch")
    if any(not result.match for result in results):
        reasons.append("expected status or evidence mismatch")
    if any(result.transport_status in _FAILED_TRANSPORT for result in results):
        reasons.append("failed transport status")
    if len(assurance_records) != len(results):
        reasons.append("assurance record count mismatch")
    for index, record in enumerate(assurance_records):
        case_id = results[index].case_id if index < len(results) else str(index)
        if record is None:
            reasons.append(f"assurance record missing for {case_id}")
            continue
        result_run_id = results[index].run_id if index < len(results) else ""
        if getattr(record, "run_id", None) and result_run_id and record.run_id != result_run_id:
            reasons.append(
                f"assurance run_id mismatch for {case_id}: "
                f"record={record.run_id} result={result_run_id}"
            )
        failed = [check.check_name for check in record.checks if check.status == "fail"]
        if failed:
            reasons.append(
                f"assurance check failed for {case_id}: {', '.join(failed)}"
            )
    if incomplete_evidence:
        reasons.append(
            "incomplete node artifact evidence: " + "; ".join(incomplete_evidence)
        )
    if not cost_known:
        reasons.append("cost unknown")
    elif total_cost_usd > cost_ceiling_usd:
        reasons.append(f"cost {total_cost_usd:.4f} exceeds ceiling {cost_ceiling_usd:.2f}")
    if degraded:
        reasons.append("degraded run")
    if any(
        result.provider_model not in (None, "") and result.provider_model != expected_model_id
        for result in results
    ):
        reasons.append("provider/model fallback detected")
    if not failure_results or any(not item.ok for item in failure_results):
        reasons.append("failure-path checks failed")
    if any(item.provider_calls != 0 for item in failure_results):
        reasons.append("failure paths invoked a provider")
    return (not reasons, reasons)


# ---------------------------------------------------------------------------
# Checksums / evidence immutability
# ---------------------------------------------------------------------------


def _write_checksums(directory: Path) -> Path:
    manifest_path = Path(directory) / "checksums.sha256"
    files = sorted(
        path.relative_to(directory).as_posix()
        for path in Path(directory).rglob("*")
        if path.is_file() and path.name != "checksums.sha256"
    )
    lines = [f"{_sha256_file(Path(directory) / rel)}  {rel}\n" for rel in files]
    _atomic_write_text(manifest_path, "".join(lines))
    return manifest_path


def _revalidate_evidence(directory: Path) -> list[str]:
    problems: list[str] = []
    manifest_path = Path(directory) / "checksums.sha256"
    if not manifest_path.is_file():
        return ["checksums.sha256 missing"]
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        digest, _, rel = line.partition("  ")
        path = Path(directory) / rel
        if not path.is_file():
            problems.append(f"missing file {rel}")
        elif _sha256_file(path) != digest:
            problems.append(f"hash mismatch {rel}")
    return problems


def _read_assurance(runtime_db_path: Path, run_id: str) -> RunAssuranceRecord | None:
    conn = sqlite3.connect(str(runtime_db_path))
    try:
        row = conn.execute(
            "SELECT record_json FROM run_assurance WHERE run_id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return RunAssuranceRecord.model_validate_json(row[0])


def _read_node_artifacts(runtime_db_path: Path, run_id: str) -> list[dict[str, Any]]:
    """Read persisted node artifacts for one run (read-only)."""
    from catalyst_agents.trace.artifacts import read_node_artifacts

    conn = sqlite3.connect(str(runtime_db_path))
    try:
        return read_node_artifacts(conn, run_id=run_id)
    finally:
        conn.close()


def _scrub_server_paths(payload: Any) -> Any:
    """Replace server/local filesystem paths in exported evidence strings."""
    if isinstance(payload, dict):
        return {key: _scrub_server_paths(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_scrub_server_paths(item) for item in payload]
    if isinstance(payload, str):
        value = payload
        for marker in ("/workspace/", "/Users/", "/workspace"):
            value = value.replace(marker, "<redacted_path>")
        return value
    return payload


def required_node_artifact_pairs(
    *,
    output_status: str | None,
    entered_judge_validator: bool,
) -> frozenset[tuple[str, str]]:
    """Return the path-specific required (node, artifact_type) pairs."""
    required = set(REQUIRED_NODE_ARTIFACT_MATRIX["base"])
    if (output_status or "").upper() == "ABSTAIN":
        required |= set(REQUIRED_NODE_ARTIFACT_MATRIX["abstain"])
    if entered_judge_validator:
        required |= set(REQUIRED_NODE_ARTIFACT_MATRIX["judge_validator"])
    return frozenset(required)


def _export_node_artifacts(
    staging_dir: Path,
    *,
    case_id: str,
    run_id: str,
    rows: list[dict[str, Any]],
    output_status: str | None = None,
    entered_judge_validator: bool = False,
) -> list[dict[str, str]]:
    """Export redacted node artifacts for one case under staging/node_artifacts.

    Returns a list of missing required pairs as
    ``{"case_id", "node", "artifact_type"}`` so the Wave 3 runner can record
    an explicit incomplete-evidence reason and block the success token.
    """
    out_dir = staging_dir / "node_artifacts" / "by_case" / case_id / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    present_types: set[str] = set()
    present_pairs: set[tuple[str, str]] = set()
    lines: list[dict[str, Any]] = []
    for row in rows:
        artifact_type = str(row.get("artifact_type") or "")
        node = str(row.get("node") or "")
        present_types.add(artifact_type)
        if node and artifact_type:
            present_pairs.add((node, artifact_type))
        lines.append({
            "run_id": row.get("run_id"),
            "event_seq": row.get("event_seq"),
            "node": node,
            "artifact_type": artifact_type,
            "created_at": row.get("created_at"),
            "payload": _scrub_server_paths(_redact_payload(row.get("payload_json", {}))),
            "case_id": case_id,
        })
    required_pairs = required_node_artifact_pairs(
        output_status=output_status,
        entered_judge_validator=entered_judge_validator,
    )
    missing_pairs = sorted(required_pairs - present_pairs)
    missing_records = [
        {"case_id": case_id, "node": node, "artifact_type": artifact_type}
        for node, artifact_type in missing_pairs
    ]
    (out_dir / "node_artifacts.jsonl").write_text(
        "".join(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n" for line in lines),
        encoding="utf-8",
    )
    (out_dir / "inventory.json").write_text(
        json.dumps(
            {
                "schema_version": "amend5_1_node_artifacts_export_v1",
                "case_id": case_id,
                "run_id": run_id,
                "artifact_types": sorted(present_types),
                "present_pairs": sorted(
                    [{"node": n, "artifact_type": t} for n, t in present_pairs],
                    key=lambda item: (item["node"], item["artifact_type"]),
                ),
                "required_pairs": sorted(
                    [{"node": n, "artifact_type": t} for n, t in required_pairs],
                    key=lambda item: (item["node"], item["artifact_type"]),
                ),
                "missing_pairs": missing_records,
                "required_artifact_types": sorted({t for _, t in required_pairs}),
                "complete": not missing_records,
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ) + "\n",
        encoding="utf-8",
    )
    return missing_records


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def run_user_smoke(
    *,
    cases: list[CasePackCase],
    run_id: str,
    output_root: Path,
    resolved: ResolvedRuntimeIdentity,
    wave2_dir: Path,
    t4_evidence_dir: Path,
    boundary: EmbeddingBoundary,
    runtime_db_path: Path,
    frozen_db_path: Path,
    http: Any,
    provider: str = DEFAULT_PROVIDER,
    model_id: str = DEFAULT_MODEL_ID,
    base_url: str | None = None,
    credential_source: str = "server_env",
    poll_interval_seconds: float = POLL_DEFAULT_INTERVAL_SECONDS,
    poll_timeout_seconds: float = POLL_DEFAULT_TIMEOUT_SECONDS,
    cost_ceiling_usd: float = COST_CEILING_USD,
    provider_validator: Callable[[], Any] | None = None,
    clock: Callable[[], str] | None = None,
) -> UserSmokeSummary:
    """Run the full user-smoke through the public HTTP surface.

    ``cases`` is the full T4 case pack; the three approved cases are selected
    internally.  All gates run before any provider call or artifact write.
    """
    if model_id in RETIRED_MODEL_ALIASES:
        raise ValueError(
            f"retired provider model alias {model_id!r} is not supported; "
            f"use {DEFAULT_MODEL_ID}"
        )
    selected = select_user_smoke_cases(cases)
    validate_embedding_boundary(boundary)
    if poll_timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("poll timeout/interval must be positive")
    if cost_ceiling_usd <= 0:
        raise ValueError("cost ceiling must be positive")

    # Gate: Wave 2 evidence (T4 + full four-arm token) before anything else.
    validated_wave2 = validate_wave2_evidence(
        wave2_dir=wave2_dir,
        t4_evidence_dir=t4_evidence_dir,
        current_case_pack=cases,
        resolved=resolved,
    )

    output_root = Path(output_root)
    final_dir = output_root / run_id
    if final_dir.exists():
        raise ValueError(f"run_id already exists: {run_id}")

    staging_dir = output_root / f".{run_id}.staging-{uuid.uuid4().hex[:12]}"
    started_at = clock() if clock is not None else _utc_now()
    try:
        staging_dir.mkdir(parents=True, exist_ok=False)
        for subdir in ("requests", "responses", "traces", "assurance", "failure_paths", "node_artifacts"):
            (staging_dir / subdir).mkdir(exist_ok=False)

        # Gate: app health readiness.
        _require_ready_health(http)

        # Gate: provider/model validation (one bounded call; adapter seam).
        if provider_validator is not None:
            provider_validator()
        else:
            _validate_provider_http(
                http,
                provider=provider,
                model_id=model_id,
                base_url=base_url,
                credential_source=credential_source,
            )

        request_index = 0
        response_index = 0
        results: list[UserSmokeCaseResult] = []
        assurance_records: list[RunAssuranceRecord | None] = []
        incomplete_evidence: list[str] = []
        total_cost = 0.0
        cost_known = True
        degraded = False

        for item in selected:
            case = item.case
            body = {
                "ticker": case.ticker,
                "trade_date": case.session_date,
                "query": case.query,
                "model": _model_config(
                    provider=provider,
                    model_id=model_id,
                    base_url=base_url,
                    credential_source=credential_source,
                ),
                "config": "mcj_full",
            }
            _atomic_write_json(
                staging_dir / "requests" / f"request-{request_index}.json",
                _redact_payload(body),
            )
            request_index += 1

            submit_resp = http.post("/api/live-runs", json=body)
            _atomic_write_json(
                staging_dir / "responses" / f"response-{response_index}.json",
                _redact_payload(submit_resp.json),
            )
            response_index += 1
            if submit_resp.status_code != 200:
                raise RunnerValidationError(
                    f"live-run submit failed HTTP {submit_resp.status_code}"
                )
            submit_payload = submit_resp.json
            if not isinstance(submit_payload, dict):
                raise RunnerValidationError("live-run submit returned a non-object payload")

            created_run_id = submit_payload.get("run_id")
            transport_status = submit_payload.get("status")
            if transport_status == "FAILED_REQUEST":
                # Typed validation failure at the public boundary (zero provider calls).
                results.append(UserSmokeCaseResult(
                    case_id=case.case_id,
                    run_id=str(created_run_id or ""),
                    transport_status="FAILED_REQUEST",
                    output_status=None,
                    expected_class=item.expected_class,
                    raw_expected_status=item.raw_expected_status,
                    match=False,
                    citations_ok=False,
                    citation_detail="run rejected before provider use",
                    latency_ms=None,
                    retry_count=None,
                    provider_model=None,
                    finish_reason=None,
                    error_reason=str((submit_payload.get("failure") or {}).get("sub_reason") or "failed_request"),
                    input_tokens=None,
                    output_tokens=None,
                    cost_usd=None,
                    provider_calls=0,
                    detail="FAILED_REQUEST",
                ))
                continue
            if not created_run_id:
                raise RunnerValidationError("live-run submit missing run_id")

            # Bounded poll until a terminal state.
            status = transport_status
            deadline = time.monotonic() + poll_timeout_seconds
            while status not in _TERMINAL_STATUSES:
                if time.monotonic() >= deadline:
                    raise RunnerValidationError(
                        f"poll timeout after {poll_timeout_seconds:.1f}s for run {created_run_id}"
                    )
                time.sleep(poll_interval_seconds)
                poll_resp = http.get(f"/api/live-runs/{created_run_id}")
                poll_payload = _require_json_response(
                    poll_resp, endpoint=f"poll live-run {created_run_id}"
                )
                status = (
                    poll_payload.get("status")
                    or poll_payload.get("lifecycle_status")
                    or "UNKNOWN"
                )

            events_resp = http.get(f"/api/live-runs/{created_run_id}/events")
            if events_resp.status_code != 200:
                raise RunnerValidationError(
                    f"events endpoint returned HTTP {events_resp.status_code}"
                )
            events = events_resp.json
            if not isinstance(events, list):
                events = []
            artifacts = http.get(f"/api/live-runs/{created_run_id}/artifacts").json
            workspace = _require_json_response(
                http.get(f"/api/live-runs/{created_run_id}/workspace"),
                endpoint=f"workspace {created_run_id}",
            )

            trace_path = staging_dir / "traces" / f"trace-{case.case_id}.json"
            try:
                export_run(created_run_id, out_path=trace_path, db_path=runtime_db_path)
            except ValueError:
                # M6 runs persist the lifecycle in run_events/run_artifacts; a
                # run that failed before graph execution has no legacy
                # agent_runs trace. Preserve a minimal trace so evidence is
                # never lost for review, but only for failed transports.
                if status not in _FAILED_TRANSPORT:
                    raise
                _atomic_write_json(
                    trace_path,
                    {
                        "run_id": created_run_id,
                        "status": status,
                        "events": [],
                        "error": "no legacy trace persisted for failed M6 run",
                    },
                )

            assurance = _read_assurance(runtime_db_path, created_run_id)
            assurance_records.append(assurance)
            assurance_path = staging_dir / "assurance" / f"assurance-{case.case_id}.json"
            if assurance is not None:
                _atomic_write_json(
                    assurance_path,
                    json.loads(assurance.model_dump_json()),
                )
            else:
                _atomic_write_json(assurance_path, {"error": "assurance record missing"})

            output_status = assurance.output_status if assurance is not None else None
            if output_status is None and isinstance(workspace.get("result"), dict):
                output_status = workspace["result"].get("output_status")
            if output_status is None and isinstance(workspace.get("attribution_status"), str):
                output_status = workspace["attribution_status"]

            node_names = {
                str(event.get("node") or "")
                for event in events
                if isinstance(event, dict)
            }
            entered_judge_validator = "judge" in node_names or "validator" in node_names
            node_artifact_rows = _read_node_artifacts(runtime_db_path, created_run_id)
            missing_artifacts = _export_node_artifacts(
                staging_dir,
                case_id=case.case_id,
                run_id=created_run_id,
                rows=node_artifact_rows,
                output_status=output_status if isinstance(output_status, str) else None,
                entered_judge_validator=entered_judge_validator,
            )
            if missing_artifacts:
                for missing in missing_artifacts:
                    incomplete_evidence.append(
                        f"{missing['case_id']}/{missing['node']}/{missing['artifact_type']}"
                    )

            case_cost, case_cost_known = _aggregate_cost(events)
            if not case_cost_known:
                cost_known = False
            total_cost += case_cost if case_cost is not None else 0.0

            provider_calls = _provider_calls_in_events(events)

            latency_ms = _run_latency_ms(workspace, events)
            retry_count = assurance.retry_count if assurance is not None else None
            tokens_in = sum(int(event.get("input_tokens") or 0) for event in events)
            tokens_out = sum(int(event.get("output_tokens") or 0) for event in events)
            provider_model = next(
                (event.get("model_id") for event in events if event.get("model_id")),
                model_id,
            )
            finish_reason = None
            if isinstance(workspace.get("result"), dict):
                finish_reason = workspace["result"].get("validation_error")
            error_reason = None
            if status in _FAILED_TRANSPORT:
                failure = workspace.get("failure") or {}
                error_reason = str(failure.get("message") or failure.get("sub_reason") or status)

            cited_ids = _cited_ids_from_workspace(workspace)
            citations_ok, citation_detail = _validate_citations(
                frozen_db_path,
                case=case,
                cited_ids=cited_ids,
                manifest_id=resolved.corpus_manifest_id,
            )

            match = (
                status not in _FAILED_TRANSPORT
                and output_status == item.expected_class
                and citations_ok
            )
            if assurance is not None and assurance.is_degraded:
                degraded = True

            results.append(UserSmokeCaseResult(
                case_id=case.case_id,
                run_id=created_run_id,
                transport_status=status,
                output_status=output_status,
                expected_class=item.expected_class,
                raw_expected_status=item.raw_expected_status,
                match=match,
                citations_ok=citations_ok,
                citation_detail=citation_detail,
                latency_ms=latency_ms,
                retry_count=retry_count,
                provider_model=provider_model,
                finish_reason=finish_reason,
                error_reason=error_reason,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cost_usd=case_cost,
                provider_calls=provider_calls,
                detail=f"output_status={output_status} transport={status}",
            ))

        failure_results = _exercise_failure_paths(
            http,
            provider=provider,
            model_id=model_id,
            base_url=base_url,
            credential_source=credential_source,
        )
        _atomic_write_json(
            staging_dir / "failure_paths" / "failure-paths.json",
            {
                "schema_version": FAILURE_PATHS_SCHEMA,
                "results": [asdict(item) for item in failure_results],
            },
        )
        _atomic_write_json(
            staging_dir / "case_results.json",
            {
                "schema_version": CASE_RESULTS_SCHEMA,
                "cases": [asdict(item) for item in results],
            },
        )

        # Secret scan must pass before the token may be written.
        secret_values: tuple[str, ...] = ()
        configured = os.environ.get("DEEPSEEK_API_KEY", "")
        if configured:
            secret_values = (configured,)
        violations = _secret_scan(staging_dir, secret_values=secret_values)
        if violations:
            raise RunnerValidationError(f"secret scan failed: {violations[:5]}")

        may_write, gate_reasons = _success_token_gate(
            selected=selected,
            results=results,
            assurance_records=assurance_records,
            incomplete_evidence=incomplete_evidence,
            failure_results=failure_results,
            cost_known=cost_known,
            total_cost_usd=total_cost,
            cost_ceiling_usd=cost_ceiling_usd,
            degraded=degraded,
            expected_model_id=model_id,
        )
        token_written = False
        completed_at = clock() if clock is not None else _utc_now()
        meta: dict[str, Any] = {
            "schema_version": META_SCHEMA_VERSION,
            "run_id": run_id,
            "git_head": resolved.git_head,
            "runtime_git_head": resolved.git_head,
            "index_build_revision": resolved.code_revision,
            "snapshot_id": resolved.snapshot_id,
            "corpus_manifest_id": resolved.corpus_manifest_id,
            "source_bundle_id": resolved.source_bundle_id,
            "probe_report_id": resolved.probe_report_id,
            "postbuild_readiness_id": resolved.postbuild_readiness_id,
            "index_manifest_id": resolved.index_manifest_id,
            "active_table_name": resolved.active_table_name,
            "model_name": resolved.model_name,
            "model_revision": resolved.model_revision,
            "tokenizer_revision": resolved.tokenizer_revision,
            "dimension": resolved.dimension,
            "dtype": resolved.dtype,
            "normalization_mode": resolved.normalization_mode,
            "vector_count": resolved.vector_count,
            "lancedb_row_count": resolved.lancedb_row_count,
            "db_path": str(resolved.db_path),
            "db_sha256": resolved.db_sha256,
            "db_user_version": resolved.db_user_version,
            "db_foreign_key_violations": resolved.db_foreign_key_violations,
            "wave2_evidence_dir": str(validated_wave2.wave2_dir),
            "t4_evidence_dir": str(validated_wave2.t4_evidence.evidence_dir),
            "t4_evidence_case_pack_id": validated_wave2.t4_evidence.case_pack_id,
            "provider": provider,
            "model_id": model_id,
            "credential_source": credential_source,
            "model_pricing_source": "repository MODEL_PRICING lock (cost_tracker.py)",
            "model_pricing_assumption": "conservative cache-miss rates",
            "model_pricing_recorded_at": "2026-08-11",
            "model_pricing_input_usd_per_million": _model_pricing_rate(model_id, "input"),
            "model_pricing_output_usd_per_million": _model_pricing_rate(model_id, "output"),
            "selected_case_ids": list(USER_SMOKE_CASE_IDS),
            "expected_classes": dict(USER_SMOKE_EXPECTED_CLASSES),
            "started_at": started_at,
            "completed_at": completed_at,
            "total_cost_usd": total_cost if cost_known else None,
            "cost_known": cost_known,
            "cost_ceiling_usd": cost_ceiling_usd,
            "poll_timeout_seconds": poll_timeout_seconds,
            "token_gate": {"written": may_write, "reasons": gate_reasons},
        }
        _atomic_write_json(staging_dir / "meta.json", meta)

        if may_write:
            token_path = staging_dir / WAVE_TOKEN_FILENAME
            _atomic_write_text(token_path, USER_SMOKE_TOKEN + "\n")
            token_written = True

        _write_checksums(staging_dir)
        os.replace(staging_dir, final_dir)
        problems = _revalidate_evidence(final_dir)
        if problems:
            raise RunnerValidationError(f"evidence revalidation failed: {problems[:5]}")

        return UserSmokeSummary(
            run_id=run_id,
            case_count=len(results),
            token_written=token_written,
            completed_at=completed_at,
            meta_path=final_dir / "meta.json",
            total_cost_usd=total_cost if cost_known else None,
            cost_known=cost_known,
            failure_path_results=failure_results,
        )
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


__all__ = [
    "COST_CEILING_USD", "DEFAULT_MODEL_ID", "DEFAULT_PROVIDER",
    "FAILURE_PATHS_SCHEMA", "META_SCHEMA_VERSION", "POLL_DEFAULT_INTERVAL_SECONDS",
    "POLL_DEFAULT_TIMEOUT_SECONDS", "USER_SMOKE_CASE_IDS", "USER_SMOKE_EXPECTED_CLASSES",
    "USER_SMOKE_TOKEN", "WAVE2_FOUR_ARM_TOKEN", "WAVE_TOKEN_FILENAME",
    "CASE_RESULTS_SCHEMA", "EXPECTED_WAVE2_CASE_COUNT", "RETIRED_MODEL_ALIASES",
    "FailurePathResult", "HttpResponse", "UserSmokeCase", "UserSmokeCaseResult",
    "UserSmokeSummary", "ValidatedWave2Evidence",
    "REQUIRED_NODE_ARTIFACT_MATRIX", "REQUIRED_NODE_ARTIFACT_TYPES",
    "_export_node_artifacts", "_model_pricing_rate", "_read_node_artifacts",
    "_redact_payload", "_scrub_server_paths", "_secret_scan",
    "normalize_expected_class", "required_node_artifact_pairs",
    "run_user_smoke", "select_user_smoke_cases", "validate_wave2_evidence",
]
