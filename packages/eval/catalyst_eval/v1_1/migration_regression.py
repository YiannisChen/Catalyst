"""T4/user-smoke + migration regression gate (M7-9).

T4 and user-smoke remain identity-bound regression/smoke suites, not
attribution quality evidence. Migration regression reads the sealed M1
baseline and M5 Gate A/B artifacts through their existing adapters, then
compares the M6 V1 app/SSE output on identical request facts. Only locked
structural invariants are compared (request facts, runtime identity,
ContextPack identity, claim lineage, status/refusal normalization,
leakage); ``comparability_declared=false`` whenever data/model/environment
identity differs. No direct quality/equality claim is ever made. Legacy eval
remains a sealed baseline reader only.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class BaselineFacts:
    """Structural facts extracted from a sealed baseline artifact."""

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
    baseline: BaselineFacts, v1: V1Facts
) -> MigrationRegressionResult:
    """Compare only the locked structural invariants; never a quality claim."""
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
    comparability = not mismatches and not identity_differ
    return MigrationRegressionResult(
        comparability_declared=comparability,
        invariant_mismatches=tuple(mismatches),
        identity_fields_differ=identity_differ,
        quality_claim_made=False,
    )


def read_baseline_facts(path: str | Path) -> BaselineFacts:
    """Read sealed baseline facts through the sealed-baseline reader contract.

    The baseline report is read-only; this function never rewrites it.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("baseline artifact must be a JSON object")
    runtime = payload.get("runtime_identity")
    if not isinstance(runtime, dict) or not runtime.get("ref") or not runtime.get("hash"):
        raise ValueError("baseline runtime_identity must contain ref and hash")
    return BaselineFacts(
        case_id=str(payload.get("case_id") or ""),
        request_facts=dict(payload.get("request_facts") or {}),
        runtime_identity_ref=str(runtime["ref"]),
        runtime_identity_hash=str(runtime["hash"]),
        context_pack_identity=str(payload.get("context_pack_identity") or ""),
        claim_lineage=tuple(payload.get("claim_lineage") or ()),
        status_normalization=payload.get("status_normalization"),
        refusal_normalization=payload.get("refusal_normalization"),
        leakage_findings=tuple(payload.get("leakage_findings") or ()),
    )


def _import_json() -> Any:
    import json

    return json


import json  # noqa: E402  (used by read_baseline_facts)

__all__ = [
    "BaselineFacts",
    "MigrationRegressionResult",
    "V1Facts",
    "compare_structural_invariants",
    "read_baseline_facts",
]
