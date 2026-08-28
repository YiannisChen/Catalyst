"""T4/user-smoke + migration regression gate (M7-9, Batch-B corrective).

T4 and user-smoke remain identity-bound regression/smoke suites, not
attribution quality evidence. Migration regression consumes the sealed M1
baseline and M5 Gate A/B artifacts through the sealed-baseline adapter
contract (``catalyst_eval.baseline.gates`` identity constants), then compares
the M6 V1 app/SSE output on identical request facts. Only locked structural
invariants are compared (request facts, runtime identity, ContextPack
identity, claim lineage, status/refusal normalization, leakage);
``comparability_declared=false`` is MANDATORY while Q-002 is unrecovered,
even when every structural invariant matches. No direct quality/equality
claim is ever made. Legacy eval remains a sealed baseline reader only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from catalyst_eval.baseline.gates import Q002_PROMOTED_ENV_REASON

SEALED_BASELINE_SCHEMA = "baseline_v1"
_MISSING_Q002 = (
    "sealed baseline must preserve Q-002 NON-COMPARABLE: "
    f"promoted_env_recovered=false with reason {Q002_PROMOTED_ENV_REASON!r}"
)


@dataclass(frozen=True)
class BaselineFacts:
    """Structural facts extracted through the sealed-baseline adapter."""

    case_id: str
    request_facts: Mapping[str, Any]
    runtime_identity_ref: str
    runtime_identity_hash: str
    context_pack_identity: str
    claim_lineage: tuple[str, ...]
    status_normalization: str | None
    refusal_normalization: str | None
    leakage_findings: tuple[str, ...]
    q002_promoted_env_recovered: bool = False
    q002_promoted_env_reason: str = Q002_PROMOTED_ENV_REASON


@dataclass(frozen=True)
class V1Facts:
    """Structural facts from the M6 V1 app/SSE output for the same request."""

    case_id: str
    request_facts: Mapping[str, Any]
    runtime_identity_ref: str
    runtime_identity_hash: str
    context_pack_identity: str
    claim_lineage: tuple[str, ...]
    status_normalization: str | None
    refusal_normalization: str | None
    leakage_findings: tuple[str, ...]


@dataclass(frozen=True)
class MigrationRegressionResult:
    comparability_declared: bool
    invariant_mismatches: tuple[str, ...]
    identity_fields_differ: bool
    quality_claim_made: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "comparability_declared": self.comparability_declared,
            "invariant_mismatches": list(self.invariant_mismatches),
            "identity_fields_differ": self.identity_fields_differ,
            "quality_claim_made": self.quality_claim_made,
        }


def _mismatch(label: str, baseline: Any, v1: Any) -> str | None:
    if baseline != v1:
        return f"{label} differs: baseline={baseline!r} v1={v1!r}"
    return None


def compare_structural_invariants(
    baseline: BaselineFacts,
    v1: V1Facts,
    *,
    q002_recovered: bool | None = None,
) -> MigrationRegressionResult:
    """Compare only the locked structural invariants; never a quality claim.

    ``q002_recovered`` defaults to the baseline's sealed Q-002 marker; while
    it is false the result MUST declare NON-COMPARABLE even when every
    structural invariant matches (Batch-B corrective).
    """
    if q002_recovered is None:
        q002_recovered = baseline.q002_promoted_env_recovered
    mismatches: list[str] = []
    if baseline.case_id != v1.case_id:
        mismatches.append(f"case_id differs: baseline={baseline.case_id} v1={v1.case_id}")

    def _check(label: str, baseline_value: Any, v1_value: Any) -> None:
        found = _mismatch(label, baseline_value, v1_value)
        if found is not None:
            mismatches.append(found)

    _check("request facts", dict(baseline.request_facts), dict(v1.request_facts))
    _check("runtime identity ref", baseline.runtime_identity_ref, v1.runtime_identity_ref)
    _check("runtime identity hash", baseline.runtime_identity_hash, v1.runtime_identity_hash)
    _check("context pack identity", baseline.context_pack_identity, v1.context_pack_identity)
    _check("claim lineage", tuple(baseline.claim_lineage), tuple(v1.claim_lineage))
    _check("status normalization", baseline.status_normalization, v1.status_normalization)
    _check("refusal normalization", baseline.refusal_normalization, v1.refusal_normalization)
    _check("leakage findings", tuple(baseline.leakage_findings), tuple(v1.leakage_findings))

    identity_differ = any(
        label in " ".join(mismatches).lower()
        for label in ("runtime identity", "request facts")
    )
    if not q002_recovered:
        mismatches.append(
            "Q-002 promoted-environment tuple is unrecovered; "
            "comparability is MANDATORY NON-COMPARABLE"
        )
    comparability = not mismatches and not identity_differ
    return MigrationRegressionResult(
        comparability_declared=comparability,
        invariant_mismatches=tuple(mismatches),
        identity_fields_differ=identity_differ,
        quality_claim_made=False,
    )


def _sealed_comparability(payload: Mapping[str, Any]) -> tuple[bool, str]:
    """Validate and return the sealed Q-002 comparability marker.

    The sealed M1 baseline is NON-COMPARABLE by lock while Q-002 is
    unrecovered; any other declaration is a sealed-artifact contradiction.
    """
    comparability = payload.get("comparability") or {}
    recovered = comparability.get("promoted_env_recovered")
    reason = comparability.get("promoted_env_reason")
    if recovered is not False or reason != Q002_PROMOTED_ENV_REASON:
        raise ValueError(_MISSING_Q002)
    return bool(recovered), str(reason)


def _runtime_identity_from(payload: Mapping[str, Any]) -> tuple[str, str]:
    """Consume the runtime identity through the sealed adapter shape.

    Prefers the typed ``runtime_identity`` dict used by the migration reader;
    falls back to the sealed M1 baseline run rows' data identity fields
    (corpus manifest ref + index manifest digest). The sealed baseline
    ``identity`` tuple may carry null data fields; the authoritative run rows
    carry the data identity that the sealed M1 adapter preserved.
    """
    runtime = payload.get("runtime_identity")
    if isinstance(runtime, dict) and runtime.get("ref") and runtime.get("hash"):
        return str(runtime["ref"]), str(runtime["hash"])
    runs = payload.get("runs")
    first_run: dict[str, Any] = {}
    if isinstance(runs, list):
        for run in runs:
            if isinstance(run, dict) and run.get("corpus_manifest_id"):
                first_run = run
                break
    identity = payload.get("identity") or {}
    ref = (
        first_run.get("corpus_manifest_id")
        or identity.get("corpus_manifest_id")
        or identity.get("runtime_identity_ref")
        or identity.get("code_git_sha")
    )
    digest = (
        first_run.get("index_manifest_id")
        or identity.get("index_manifest_id")
        or identity.get("data_runtime_identity_hash")
        or identity.get("integration_commit_sha")
    )
    if ref and digest:
        return str(ref), str(digest)
    raise ValueError("baseline runtime_identity must contain ref and hash")


def read_baseline_facts(path: str | Path) -> BaselineFacts:
    """Read sealed baseline facts through the sealed-baseline adapter.

    The baseline report is read-only; this function never rewrites it. A
    non-sealed artifact (missing Q-002 NON-COMPARABLE marker) fails closed.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("baseline artifact must be a JSON object")
    if payload.get("schema_version") != SEALED_BASELINE_SCHEMA:
        raise ValueError(
            f"baseline artifact must use sealed schema {SEALED_BASELINE_SCHEMA!r}, "
            f"got {payload.get('schema_version')!r}"
        )
    recovered, reason = _sealed_comparability(payload)
    runtime_ref, runtime_hash = _runtime_identity_from(payload)
    runs = payload.get("runs")
    case_ids: list[str] = []
    if isinstance(runs, list):
        for run in runs:
            if not isinstance(run, dict):
                continue
            case_id = run.get("case_id") or run.get("run_id")
            if case_id:
                case_ids.append(str(case_id))
    return BaselineFacts(
        case_id=case_ids[0] if case_ids else "",
        request_facts=dict(payload.get("request_facts") or {}),
        runtime_identity_ref=runtime_ref,
        runtime_identity_hash=runtime_hash,
        context_pack_identity=str(payload.get("context_pack_identity") or ""),
        claim_lineage=tuple(payload.get("claim_lineage") or ()),
        status_normalization=payload.get("status_normalization"),
        refusal_normalization=payload.get("refusal_normalization"),
        leakage_findings=tuple(payload.get("leakage_findings") or ()),
        q002_promoted_env_recovered=recovered,
        q002_promoted_env_reason=reason,
    )


def read_m5_gate_a_q002(path: str | Path) -> dict[str, Any]:
    """Consume the sealed M5 Gate A artifact's Q-002 marker.

    Gate A re-verifies the sealed baseline artifact chain; its Q-002 marker
    must agree with the sealed baseline (promoted_env_recovered=false).
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("M5 Gate A artifact must be a JSON object")
    if payload.get("schema_version") != "v1_1_m5_gate_a_v1":
        raise ValueError(
            f"M5 Gate A artifact must use schema v1_1_m5_gate_a_v1, got "
            f"{payload.get('schema_version')!r}"
        )
    q002 = payload.get("q002_non_comparable") or {}
    if (
        q002.get("promoted_env_recovered") is not False
        or q002.get("promoted_env_reason") != Q002_PROMOTED_ENV_REASON
    ):
        raise ValueError(_MISSING_Q002)
    return dict(q002)


__all__ = [
    "BaselineFacts",
    "MigrationRegressionResult",
    "V1Facts",
    "compare_structural_invariants",
    "read_baseline_facts",
    "read_m5_gate_a_q002",
]
