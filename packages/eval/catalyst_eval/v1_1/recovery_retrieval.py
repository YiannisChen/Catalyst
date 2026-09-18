"""M8-B pointer-free candidate-FTS retrieval-quality gate.

Ranks the benchmark cases against one exact INACTIVE candidate build through
``retrieve_lexical(..., inactive_build_id=...)`` and scores the frozen ranking
with the sealed M7 ``retrieval_metrics`` formulas. It never reads or writes
active serving pointers, never promotes a generation, and never exposes
benchmark expectations (expected evidence, judgments, oracle status,
attribution) to the retrieval call.

An empty denominator is ``NOT_EXERCISED`` and fails the gate: a metric that was
never exercised is not a pass.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from catalyst_eval.benchmark.pool_manifest import PoolArm, PoolManifest
from catalyst_eval.v1_1.case import GoldenCase
from catalyst_eval.v1_1.retrieval_metrics import (
    RetrievalMetrics,
    RetrievalResult,
    compute_retrieval_metrics,
)

SCHEMA_VERSION = "v1_1_recovery_retrieval_v1"
CANDIDATE_ARM_VERSION = "1.0.0"

# M8-B (executor lock §6). Thresholds are the frozen STAGE1_RETRIEVAL_GATES
# values plus the required-primary citable-coverage rule.
M8B_GATES = {
    "recall_at_8": 0.75,
    "primary_source_hit": 0.80,
    "duplicate_adjusted_precision": 0.60,
}

CITABLE_STATES = frozenset({"FULL_TEXT"})

# The retrieval call receives only these benchmark fields.
CALLABLE_CASE_FIELDS = ("question", "ticker", "cutoff", "session_date", "case_id")


class RecoveryRetrievalError(RuntimeError):
    pass


@dataclass(frozen=True)
class RankedCase:
    case_id: str
    question: str
    ticker: str
    cutoff: str
    session_date: str
    pool: PoolManifest
    ranked_evidence_ids: tuple[str, ...]
    included_evidence_ids: tuple[str, ...]
    ticker_violations: tuple[str, ...]
    cutoff_violations: tuple[str, ...]
    expected_primary: bool = False

    @property
    def citable_body_count(self) -> int:
        return len(self.included_evidence_ids)


@dataclass(frozen=True)
class RecoveryRetrievalReport:
    schema_version: str
    corpus_manifest_id: str
    build_id: str
    fts_digest: str | None
    case_list_sha256: str
    db_sha256: str
    db_size_bytes: int
    top_k: int
    cases: tuple[RankedCase, ...]
    metrics: RetrievalMetrics
    gate_status: Mapping[str, str]
    gate_passed: bool
    reported_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "corpus_manifest_id": self.corpus_manifest_id,
            "build_id": self.build_id,
            "fts_digest": self.fts_digest,
            "case_list_sha256": self.case_list_sha256,
            "db_sha256": self.db_sha256,
            "db_size_bytes": self.db_size_bytes,
            "top_k": self.top_k,
            "reported_at": self.reported_at,
            "gate_passed": self.gate_passed,
            "gate_status": dict(sorted(self.gate_status.items())),
            "metrics": self.metrics.as_dict(),
            "cases": [
                {
                    "case_id": case.case_id,
                    "pool_id": case.pool.pool_id,
                    "ranked_evidence_ids": list(case.ranked_evidence_ids),
                    "included_evidence_ids": list(case.included_evidence_ids),
                    "citable_body_count": case.citable_body_count,
                    "ticker_violations": list(case.ticker_violations),
                    "cutoff_violations": list(case.cutoff_violations),
                }
                for case in self.cases
            ],
        }


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _canonical_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def case_query(case: GoldenCase) -> dict[str, str]:
    """The only benchmark fields ever handed to the retrieval boundary."""
    cutoff = case.cutoff
    if hasattr(cutoff, "strftime") and hasattr(cutoff, "tzinfo"):
        cutoff_text = cutoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") \
            if cutoff.tzinfo is not None else cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:  # pragma: no cover - defensive
        cutoff_text = str(cutoff)
    return {
        "case_id": case.case_id,
        "question": case.question,
        "ticker": case.ticker,
        "cutoff": cutoff_text,
        "session_date": case.session_date,
    }


def _pool_manifest(
    *, case_id: str, ranked: Sequence[str], corpus_manifest_id: str, build_id: str,
    top_k: int,
) -> PoolManifest:
    source_artifact_id = hashlib.sha256(
        _canonical_bytes(
            {
                "case_id": case_id,
                "build_id": build_id,
                "corpus_manifest_id": corpus_manifest_id,
                "ranked_evidence_ids": list(ranked),
            }
        )
    ).hexdigest()
    payload = {
        "schema_version": "1.0.0",
        "case_id": case_id,
        "arms": [PoolArm(arm="lexical", version=CANDIDATE_ARM_VERSION, top_k=top_k)],
        "chunk_inventory": sorted(set(ranked)),
        "corpus_manifest_id": corpus_manifest_id,
        "index_manifest_id": None,
        "source_artifact_id": source_artifact_id,
    }
    pool_id = hashlib.sha256(
        _canonical_bytes(
            {
                "schema_version": payload["schema_version"],
                "case_id": case_id,
                "arms": [
                    {"arm": "lexical", "version": CANDIDATE_ARM_VERSION, "top_k": top_k}
                ],
                "chunk_inventory": payload["chunk_inventory"],
                "corpus_manifest_id": corpus_manifest_id,
                "index_manifest_id": None,
                "source_artifact_id": source_artifact_id,
            }
        )
    ).hexdigest()
    return PoolManifest(
        schema_version="1.0.0",
        pool_id=pool_id,
        case_id=case_id,
        arms=(PoolArm(arm="lexical", version=CANDIDATE_ARM_VERSION, top_k=top_k),),
        chunk_inventory=tuple(payload["chunk_inventory"]),
        corpus_manifest_id=corpus_manifest_id,
        index_manifest_id=None,
        source_artifact_id=source_artifact_id,
        created_at=datetime.now(timezone.utc).replace(microsecond=0),
    )


def _citable_chunk_ids(
    conn: sqlite3.Connection, *, build_id: str, chunk_ids: Sequence[str]
) -> tuple[str, ...]:
    """Evidence ids whose candidate body is citable FULL_TEXT."""
    unique = tuple(dict.fromkeys(chunk_ids))
    if not unique:
        return ()
    placeholders = ",".join("?" for _ in unique)
    rows = conn.execute(
        "SELECT chunk_id FROM corpus_build_chunks "
        f"WHERE build_id=? AND chunk_id IN ({placeholders}) "
        "AND content_state='FULL_TEXT' AND content_text IS NOT NULL "
        "AND length(trim(content_text)) > 0",
        (build_id, *unique),
    ).fetchall()
    present = {str(row[0]) for row in rows}
    return tuple(chunk_id for chunk_id in unique if chunk_id in present)


def default_retrieve(
    conn: sqlite3.Connection,
    *,
    corpus_manifest_id: str,
    build_id: str,
    top_k: int,
    candidate_depth: int,
) -> Callable[[Mapping[str, str]], Sequence[str]]:
    """Pointer-free candidate lexical retrieval bound to one inactive build."""
    from catalyst_data.retrieval.fts5 import retrieve_lexical

    def _retrieve(query: Mapping[str, str]) -> Sequence[str]:
        result = retrieve_lexical(
            conn,
            query["question"],
            ticker=query["ticker"],
            cutoff=query["cutoff"],
            requested_manifest_id=corpus_manifest_id,
            top_k=top_k,
            candidate_depth=candidate_depth,
            session_date=query["session_date"],
            inactive_build_id=build_id,
        )
        return tuple(item.chunk_id for item in result.results)

    return _retrieve


def rank_cases(
    *,
    cases: Sequence[GoldenCase],
    retrieve: Callable[[Mapping[str, str]], Sequence[str]],
    conn: sqlite3.Connection,
    corpus_manifest_id: str,
    build_id: str,
    top_k: int = 8,
) -> tuple[RankedCase, ...]:
    """Freeze ranked identities BEFORE any gold field is read."""
    ranked_cases: list[RankedCase] = []
    for case in cases:
        query = case_query(case)
        ranked = tuple(retrieve(query))[:top_k]
        pool = _pool_manifest(
            case_id=case.case_id,
            ranked=ranked,
            corpus_manifest_id=corpus_manifest_id,
            build_id=build_id,
            top_k=top_k,
        )
        included = _citable_chunk_ids(conn, build_id=build_id, chunk_ids=ranked)
        ranked_cases.append(
            RankedCase(
                case_id=case.case_id,
                question=query["question"],
                ticker=query["ticker"],
                cutoff=query["cutoff"],
                session_date=query["session_date"],
                pool=pool,
                ranked_evidence_ids=ranked,
                included_evidence_ids=included,
                ticker_violations=(),
                cutoff_violations=(),
            )
        )
    return tuple(ranked_cases)


def evaluate_recovery_retrieval_gates(
    ranked_cases: Sequence[RankedCase],
    metrics: RetrievalMetrics,
) -> dict[str, str]:
    """PASS / FAIL / NOT_EXERCISED for every M8-B gate."""
    status: dict[str, str] = {}
    metric_by_gate = {
        "recall_at_8": metrics.recall_at_k,
        "primary_source_hit": metrics.primary_source_hit,
        "duplicate_adjusted_precision": metrics.duplicate_adjusted_precision,
    }
    for gate, threshold in M8B_GATES.items():
        metric = metric_by_gate[gate]
        if metric.denominator == 0 or metric.value is None:
            status[gate] = "NOT_EXERCISED"
            continue
        status[gate] = "PASS" if metric.value >= threshold else "FAIL"
    violations = metrics.violations
    status["ticker_or_cutoff_violations"] = (
        "NOT_EXERCISED"
        if violations.denominator == 0 and not violations.case_ids
        else ("PASS" if violations.numerator == 0 else "FAIL")
    )
    required_primary = [case for case in ranked_cases if case.expected_primary]
    if not required_primary:
        status["required_primary_citable"] = "NOT_EXERCISED"
    else:
        missing = [c.case_id for c in required_primary if not c.included_evidence_ids]
        status["required_primary_citable"] = "PASS" if not missing else "FAIL"
    return status


def frozen_ranked_payload(ranked_cases: Sequence[RankedCase]) -> dict[str, Any]:
    """PoolManifest-compatible frozen ranking (no gold-derived fields)."""
    return {
        "schema_version": "v1_1_recovery_ranked_pools_v1",
        "cases": [
            {
                "case_id": case.case_id,
                "pool": json.loads(case.pool.model_dump_json()),
                "ranked_evidence_ids": list(case.ranked_evidence_ids),
            }
            for case in ranked_cases
        ],
    }


def _write_frozen_ranked(
    path: str | Path, ranked_cases: Sequence[RankedCase]
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_canonical_bytes(frozen_ranked_payload(ranked_cases)))


def run_candidate_fts_retrieval(
    *,
    db_path: str | Path,
    corpus_manifest_id: str,
    build_id: str,
    cases: Sequence[GoldenCase],
    expected_primary_case_ids: Iterable[str] = (),
    top_k: int = 8,
    candidate_depth: int = 20,
    retrieve: Callable[[Mapping[str, str]], Sequence[str]] | None = None,
    frozen_ranked_path: str | Path | None = None,
) -> RecoveryRetrievalReport:
    """Rank + score one inactive candidate FTS generation (read-only)."""
    path = Path(db_path)
    if not path.is_file():
        raise RecoveryRetrievalError(f"candidate derivative not found: {path}")
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size_bytes += len(block)
    db_sha256 = digest.hexdigest()
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        retrieve_fn = retrieve or default_retrieve(
            conn,
            corpus_manifest_id=corpus_manifest_id,
            build_id=build_id,
            top_k=top_k,
            candidate_depth=candidate_depth,
        )
        ranked = rank_cases(
            cases=cases,
            retrieve=retrieve_fn,
            conn=conn,
            corpus_manifest_id=corpus_manifest_id,
            build_id=build_id,
            top_k=top_k,
        )
        if frozen_ranked_path is not None:
            # Freeze the PoolManifest-compatible ranked identities before any
            # gold-derived field is used by the scorer.
            _write_frozen_ranked(frozen_ranked_path, ranked)
        required = set(expected_primary_case_ids)
        if not required:
            required = {case.case_id for case in cases if case.expected_primary_evidence}
        ranked = tuple(
            replace(case, expected_primary=case.case_id in required) for case in ranked
        )
        metrics = compute_retrieval_metrics(
            [
                RetrievalResult(
                    case_id=case.case_id,
                    pool=case.pool,
                    ranked_evidence_ids=case.ranked_evidence_ids,
                    ticker_violations=case.ticker_violations,
                    cutoff_violations=case.cutoff_violations,
                )
                for case in ranked
            ],
            cases,
            top_k=top_k,
        )
    finally:
        conn.close()
    gate_status = evaluate_recovery_retrieval_gates(ranked, metrics)
    gate_passed = bool(gate_status) and all(
        value == "PASS" for value in gate_status.values()
    )
    return RecoveryRetrievalReport(
        schema_version=SCHEMA_VERSION,
        corpus_manifest_id=corpus_manifest_id,
        build_id=build_id,
        fts_digest=None,
        case_list_sha256=hashlib.sha256(
            _canonical_bytes([case.case_id for case in cases])
        ).hexdigest(),
        db_sha256=db_sha256,
        db_size_bytes=size_bytes,
        top_k=top_k,
        cases=ranked,
        metrics=metrics,
        gate_status=gate_status,
        gate_passed=gate_passed,
        reported_at=_canonical_utc_now(),
    )


__all__ = [
    "CALLABLE_CASE_FIELDS",
    "CITABLE_STATES",
    "M8B_GATES",
    "RankedCase",
    "RecoveryRetrievalError",
    "RecoveryRetrievalReport",
    "SCHEMA_VERSION",
    "case_query",
    "default_retrieve",
    "evaluate_recovery_retrieval_gates",
    "frozen_ranked_payload",
    "rank_cases",
    "run_candidate_fts_retrieval",
]
