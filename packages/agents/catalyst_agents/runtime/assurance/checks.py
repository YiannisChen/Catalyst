from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from catalyst_agents.attribution.claims import ClaimRole
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


# ---------------------------------------------------------------------------
# M5-8 / C2: structural assurance (Final TSD §13; Phase 4 TSD §31)
# ---------------------------------------------------------------------------

_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")


def derive_writer_input_hash(writer_input: Any) -> str:
    """Canonical input hash for a WriterInput (locked canonical JSON rules)."""
    import hashlib

    from catalyst_agents.attribution.context_pack_builder import canonical_context_pack_json

    return hashlib.sha256(
        canonical_context_pack_json(writer_input.model_dump(mode="json"))
    ).hexdigest()


def derive_answer_output_hash(answer_text: str) -> str:
    """Canonical output hash for the provisional Answer text.

    Matches the delta-sink convention (SHA-256 of the UTF-8 answer bytes) so
    assurance binds the persisted AnswerArtifact.text_sha256 exactly.
    """
    import hashlib

    return hashlib.sha256(answer_text.encode("utf-8")).hexdigest()


def _valid_hex64(value: Any) -> bool:
    return isinstance(value, str) and _HEX64_RE.fullmatch(value) is not None


def run_structural_assurance(run_id: str, artifacts: dict[str, Any]) -> list[AssuranceCheck]:
    """Deterministic fail-closed structural checks over a streamed answer.

    Guarantees: valid stream completion; allowed claim/citation marker subsets
    with same-run citation resolution (markers REQUIRED when the plan carries
    claims, with a preserved explicit ABSTAIN exemption); required
    sections/limitations; status/type equality with the ValidatedClaimPlan;
    recomputed canonical input/output hashes and bound plan/evidence/runtime
    identity; cancellation/timeout/token/completion metadata consistency.
    Never rewrites output and never calls a model.
    """
    from catalyst_agents.runtime.assurance.record import STRUCTURAL_CHECK_ORDER

    checked_at = artifacts.get("checked_at") or "1970-01-01T00:00:00Z"
    answer_text = artifacts.get("answer_text", "")
    emitted_citations = set(artifacts.get("emitted_citations") or [])
    permitted_evidence = set(artifacts.get("permitted_evidence_ids") or [])
    emitted_claim_markers = set(artifacts.get("emitted_claim_markers") or [])
    permitted_claims = set(artifacts.get("permitted_claim_ids") or [])
    required_sections = tuple(artifacts.get("required_sections") or [])
    required_limitations = tuple(artifacts.get("required_limitations") or [])
    emitted_status = artifacts.get("emitted_status")
    emitted_type = artifacts.get("emitted_attribution_type")
    validated_status = artifacts.get("validated_status")
    validated_type = artifacts.get("validated_attribution_type")
    writer_input = artifacts.get("writer_input")
    validated_plan = artifacts.get("validated_plan")

    stream_complete = bool(artifacts.get("stream_complete"))
    answer_upper = answer_text.upper()

    # Required PRIMARY coverage: every required PRIMARY claim marker must occur
    # in the streamed answer and every required PRIMARY citation binding must
    # resolve to same-run permitted evidence. Optional SECONDARY/CONTEXT marker
    # subsets remain allowed (TSD §13). The fixed-ABSTAIN answer is explicitly
    # exempt (no cause accepted -> no required markers).
    abstain = validated_status == "ABSTAIN"
    required_primary_ids: set[str] = set()
    required_primary_citations: set[str] = set()
    if validated_plan is not None and not abstain:
        required_primary_ids = {
            claim.claim_id
            for claim in validated_plan.claims
            if claim.role is ClaimRole.PRIMARY
        }
        required_primary_citations = {
            citation
            for claim in validated_plan.claims
            if claim.role is ClaimRole.PRIMARY
            for citation in claim.citation_evidence_ids
        }

    citation_ok = (
        emitted_citations.issubset(permitted_evidence)
        and required_primary_citations.issubset(emitted_citations)
    )
    claim_markers_ok = (
        emitted_claim_markers.issubset(permitted_claims)
        and required_primary_ids.issubset(emitted_claim_markers)
    )

    sections_ok = all(section in answer_upper for section in required_sections)
    limitations_ok = all(
        limitation.lower() in answer_text.lower() for limitation in required_limitations
    )
    status_ok = emitted_status == validated_status and emitted_type == validated_type

    # ---- canonical hash coherence (recomputed, never trusted as given) -----
    input_hash = artifacts.get("input_hash")
    plan_hash = artifacts.get("plan_hash")
    evidence_state_hash = artifacts.get("evidence_state_hash")
    runtime_identity = artifacts.get("runtime_identity")
    bound_runtime_identity = artifacts.get("bound_runtime_identity")
    output_hash = artifacts.get("output_hash")
    answer_text_sha256 = artifacts.get("answer_text_sha256")

    hashes_ok = True
    for name, value in (
        ("input_hash", input_hash),
        ("plan_hash", plan_hash),
        ("evidence_state_hash", evidence_state_hash),
        ("output_hash", output_hash),
    ):
        if not _valid_hex64(value):
            hashes_ok = False
            break
    if isinstance(runtime_identity, str) and isinstance(bound_runtime_identity, str):
        if runtime_identity != bound_runtime_identity:
            hashes_ok = False
    else:
        hashes_ok = False

    if hashes_ok and writer_input is not None:
        hashes_ok = input_hash == derive_writer_input_hash(writer_input)
    if hashes_ok and validated_plan is not None:
        hashes_ok = plan_hash == validated_plan.plan_hash
        hashes_ok = hashes_ok and evidence_state_hash == validated_plan.evidence_state_hash
    if hashes_ok:
        hashes_ok = output_hash == derive_answer_output_hash(answer_text)
    if hashes_ok and answer_text_sha256 is not None:
        hashes_ok = output_hash == answer_text_sha256

    # ---- metadata consistency ----------------------------------------------
    metadata_ok = True
    completion_state = artifacts.get("completion_state")
    if stream_complete and completion_state != "completed":
        metadata_ok = False
    if not stream_complete and completion_state == "completed":
        metadata_ok = False
    if artifacts.get("cancellation_requested") and completion_state == "completed":
        metadata_ok = False
    if artifacts.get("timed_out") and completion_state == "completed":
        metadata_ok = False
    tokens = [artifacts.get("input_tokens", 0), artifacts.get("output_tokens", 0)]
    if any(not isinstance(token, int) or token < 0 for token in tokens):
        metadata_ok = False

    checks = {
        "stream_complete": _check("stream_complete", stream_complete, "provider stream reached a valid terminal completion", checked_at=checked_at),
        "citation_resolution": _check("citation_resolution", citation_ok, "every emitted citation resolves to permitted same-run evidence and every required PRIMARY citation is emitted", checked_at=checked_at),
        "claim_markers_subset": _check("claim_markers_subset", claim_markers_ok, "emitted claim markers are a subset of permitted claims and every required PRIMARY marker is emitted", checked_at=checked_at),
        "required_sections": _check("required_sections", sections_ok, "all required sections are present", checked_at=checked_at),
        "required_limitations": _check("required_limitations", limitations_ok, "all required limitations are present", checked_at=checked_at),
        "status_type_alignment": _check("status_type_alignment", status_ok, "emitted status/type equals the validated plan", checked_at=checked_at),
        "hash_coherence": _check("hash_coherence", hashes_ok, "recomputed input/plan/evidence/runtime/output hashes cohere and bind the validated plan", checked_at=checked_at),
        "metadata_consistency": _check("metadata_consistency", metadata_ok, "cancellation/timeout/token/completion metadata is consistent", checked_at=checked_at),
    }
    return [checks[name] for name in STRUCTURAL_CHECK_ORDER]
