from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from catalyst_agents.runtime.assurance.record import AssuranceCheck, CHECK_ORDER


def _check(name: str, passed: bool, detail: str, *, checked_at: str, na: bool = False) -> AssuranceCheck:
    status = "not_applicable" if na else ("pass" if passed else "fail")
    return AssuranceCheck(check_name=name, status=status, detail=detail, checked_at=checked_at)


def compute_source_flags(evidence: list[dict[str, Any]]) -> dict[str, bool]:
    valid = [item for item in evidence if item.get("valid_support", True)]
    sources = [str(item.get("source_class", "")) for item in valid]
    return {
        "opinion_only_support": bool(sources) and all(source == "analysis_opinion" for source in sources),
        "unknown_origin_support": any(source == "aggregated_unknown" for source in sources),
        "issuer_claim_only_support": bool(sources) and all(source in {"issuer_disclosure", "corporate_press_release"} for source in sources),
    }


def run_all_checks(run_id: str, artifacts: dict[str, Any]) -> list[AssuranceCheck]:
    checked_at = artifacts.get("checked_at") or "1970-01-01T00:00:00Z"
    citations = set(artifacts.get("citations") or [])
    evidence_ids = {item.get("chunk_id") for item in artifacts.get("evidence", [])}
    visible_raw = artifacts.get("judge_visible_ids")
    judge_visible = set(visible_raw or [])
    gate_results = artifacts.get("gate_results") or []
    status = artifacts.get("output_status")
    cutoff_observations = [value for value in artifacts.get("cutoff_observations", ()) if value]
    cutoff_ok = bool(artifacts.get("cutoff")) and bool(cutoff_observations) and all(
        value == artifacts.get("cutoff") for value in cutoff_observations
    )
    no_hypotheses_expected = status in {"ABSTAIN", "SYSTEM_ERROR"} and not gate_results
    checks = {
        "cutoff": _check("cutoff", cutoff_ok, "context, retrieval, validator, and final cutoff observations agree", checked_at=checked_at),
        "citation_resolution": _check("citation_resolution", citations.issubset(evidence_ids), "all citations resolve to retrieved evidence", checked_at=checked_at),
        "judge_visibility": _check("judge_visibility", visible_raw is not None and citations.issubset(judge_visible), "Judge saw every cited evidence item", checked_at=checked_at, na=no_hypotheses_expected),
        "prerequisite_gates": _check("prerequisite_gates", bool(gate_results) and all(bool(row[1]) for row in gate_results), "all emitted hypotheses passed prerequisite gates", checked_at=checked_at, na=no_hypotheses_expected),
        "legal_path": _check("legal_path", bool(artifacts.get("legal_path_ok")), "terminal status followed legal path", checked_at=checked_at),
        "trace_completeness": _check("trace_completeness", bool(artifacts.get("trace_complete")), "persisted trace is contiguous and identity-consistent", checked_at=checked_at),
        "identities": _check(
            "identities",
            artifacts.get("corpus_manifest_id") == artifacts.get("retrieval_corpus_manifest_id")
            and artifacts.get("index_manifest_id") == artifacts.get("retrieval_index_manifest_id"),
            "retriever/cutoff/manifest identities match",
            checked_at=checked_at,
        ),
        "budget_retry_repair": _check("budget_retry_repair", int(artifacts.get("repair_count", 0)) <= 1 and int(artifacts.get("retry_count", 0)) >= 0, "repair and retry counts within policy", checked_at=checked_at),
        "degraded_state": _check("degraded_state", status in {"PARTIAL", "ABSTAIN"} or not bool(artifacts.get("is_degraded", False)), "degraded state matches status", checked_at=checked_at),
        "structured_context_support": _check("structured_context_support", bool(artifacts.get("context_artifact")), "context artifact persisted", checked_at=checked_at, na=status == "SYSTEM_ERROR" and not artifacts.get("context_artifact")),
    }
    return [checks[name] for name in CHECK_ORDER]


def canonical_created_at() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
