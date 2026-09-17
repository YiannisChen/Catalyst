"""Sealed human output audit for Stage-1 (M7-6).

``v1_1_stage1_output_audit_v1`` is the ONLY source for citation correctness,
causal support, and unsupported-material decisions; LLM judges are separate
diagnostics. The audit is accepted only when every material claim is audited,
identities match the sealed run artifacts, and adjudication is resolved.
Generated claims/artifacts are immutable during audit.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from catalyst_eval.v1_1.case import GoldenCase

OUTPUT_AUDIT_SCHEMA_VERSION = "v1_1_stage1_output_audit_v1"
_SHA256_RE = __import__("re").compile(r"[0-9a-f]{64}\Z")

# Typed human vocabularies: the sealed audit may only use these values
# (Batch-B corrective). LLM judges remain separate diagnostics.
AuditDecisionV1 = Literal["SUPPORT", "PARTIAL_SUPPORT", "UNSUPPORTED"]
AuditReasonCodeV1 = Literal[
    "citations_resolve",
    "partial_citation_support",
    "no_causal_support",
    "citation_mismatch",
    "contradicted_by_evidence",
    "mechanism_unsubstantiated",
    # Supports an observed runtime boundary fact, not causal evidence.
    "runtime_limitation_verified",
]


def supported_citation_ids_for(
    decision: "AuditClaimDecision",
) -> tuple[str, ...]:
    """The supported citation units for one audited claim.

    ``supported_citation_ids`` is the explicit human record when present;
    SUPPORT derives all citation units as supported and UNSUPPORTED derives
    none. Citation correctness counts units, never claims.
    """
    if decision.supported_citation_ids:
        return decision.supported_citation_ids
    if decision.decision == "SUPPORT":
        return decision.citation_ids
    if decision.decision == "PARTIAL_SUPPORT":
        # Human must record which units actually resolve; empty means none.
        return ()
    return ()


class AuditClaimDecision(BaseModel):
    """One audited claim decision; never generated or inferred by code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    material: StrictBool
    citation_ids: tuple[str, ...] = ()
    supported_citation_ids: tuple[str, ...] = ()
    decision: AuditDecisionV1
    reason_code: AuditReasonCodeV1

    @model_validator(mode="after")
    def _citation_units_are_bounded(self) -> "AuditClaimDecision":
        if set(self.supported_citation_ids) - set(self.citation_ids):
            raise ValueError(
                "supported_citation_ids must reference only cited units"
            )
        if self.decision == "SUPPORT" and self.supported_citation_ids:
            # A SUPPORT decision supports every cited unit by definition.
            if set(self.supported_citation_ids) != set(self.citation_ids):
                raise ValueError(
                    "SUPPORT requires every cited unit supported"
                )
        if self.reason_code == "runtime_limitation_verified":
            if self.decision != "SUPPORT":
                raise ValueError(
                    "runtime_limitation_verified requires SUPPORT"
                )
            if self.material is not False:
                raise ValueError(
                    "runtime_limitation_verified requires material=false"
                )
            if self.citation_ids or self.supported_citation_ids:
                raise ValueError(
                    "runtime_limitation_verified requires no citations"
                )
        return self


class Stage1OutputAudit(BaseModel):
    """Sealed per-case human output audit (JSONL row)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = OUTPUT_AUDIT_SCHEMA_VERSION
    eval_id: str
    case_id: str
    run_manifest_id: str
    run_manifest_hash: str
    result_artifact_id: str
    result_artifact_hash: str
    auditor_id: str
    audited_at: datetime
    adjudication_state: str = "resolved"
    claims: tuple[AuditClaimDecision, ...] = ()

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: str) -> str:
        if value != OUTPUT_AUDIT_SCHEMA_VERSION:
            raise ValueError(
                f"schema must be {OUTPUT_AUDIT_SCHEMA_VERSION}, got {value!r}"
            )
        return value

    @field_validator("run_manifest_hash", "result_artifact_hash")
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value



def validate_audit_against_ledger(
    audits: Sequence[Stage1OutputAudit],
    *,
    eval_manifest: Any,
    gold_cases: Sequence[GoldenCase],
    ledger: Any,
    stratification: Mapping[str, Any] | None = None,
) -> bool:
    """Full CLI audit validation against the ledger/run artifacts.

    Every audit row must match a sealed identity-valid COMPLETED ledger row
    (run_manifest id/hash and result artifact id/hash) and every run claim
    (material and cited units) must be audited. Raises ValueError on any
    mismatch; never authors human decisions.
    """
    eval_id = eval_manifest.evaluation_identity.eval_id
    ordered_ids = list(eval_manifest.evaluation_identity.ordered_case_ids)
    gold_by_case = {case.case_id: case for case in gold_cases}
    if set(ordered_ids) != set(gold_by_case):
        raise ValueError("gold cases must cover the eval manifest case ids")
    audit_by_case = {audit.case_id: audit for audit in audits}
    if set(audit_by_case) != set(ordered_ids):
        missing = sorted(set(ordered_ids) - set(audit_by_case))
        extra = sorted(set(audit_by_case) - set(ordered_ids))
        raise ValueError(
            "audit rows must cover the ordered cases one-to-one "
            f"(missing={missing} extra={extra})"
        )

    rows_by_case: dict[str, Any] = {}
    for row in ledger.rows:
        if row.eval_id != eval_id:
            continue
        if row.case_id not in rows_by_case:
            rows_by_case[row.case_id] = row
    for case_id in ordered_ids:
        row = rows_by_case.get(case_id)
        if row is None or row.terminal_status != "COMPLETED" or not row.identity_valid:
            raise ValueError(
                f"audit requires an identity-valid COMPLETED ledger row for "
                f"{case_id!r}"
            )
        audit = audit_by_case[case_id]
        gold = gold_by_case[case_id]
        if audit.eval_id != eval_id:
            raise ValueError(
                f"audit eval_id mismatch for {case_id!r}: audit={audit.eval_id} "
                f"expected={eval_id}"
            )
        if audit.run_manifest_id != row.run_manifest_id:
            raise ValueError(
                f"audit run_manifest_id mismatch for {case_id!r}: "
                f"audit={audit.run_manifest_id} ledger={row.run_manifest_id}"
            )
        if audit.run_manifest_hash != row.run_manifest_hash:
            raise ValueError(
                f"audit run_manifest_hash mismatch for {case_id!r}"
            )
        if audit.result_artifact_id != row.result_artifact_id:
            raise ValueError(
                f"audit result_artifact_id mismatch for {case_id!r}"
            )
        if audit.result_artifact_hash != row.result_artifact_hash:
            raise ValueError(
                f"audit result_artifact_hash mismatch for {case_id!r}"
            )
        run_output = _run_output_from_ledger_row(row, gold)
        validate_output_audit(
            audit,
            run_output,
            gold,
            eval_id=eval_id,
            run_manifest_id=row.run_manifest_id,
            run_manifest_hash=row.run_manifest_hash,
            result_artifact_id=row.result_artifact_id,
            result_artifact_hash=row.result_artifact_hash,
        )
    return True


def _run_output_from_ledger_row(row: Any, gold: GoldenCase) -> Any:
    """Rebuild the immutable run output from the sealed ledger run facts."""
    from catalyst_eval.v1_1.attribution_metrics import (
        RunAttributionOutput,
        RunClaimOutput,
    )

    facts = row.run_facts or {}
    from catalyst_eval.v1_1.run_facts import validate_run_facts

    validated = validate_run_facts(
        facts,
        expected_case_id=gold.case_id,
        row_provider_calls=row.provider_calls,
    )
    claims = tuple(
        RunClaimOutput(
            claim_id=claim.claim_id,
            material=claim.material,
            citation_ids=tuple(claim.citation_ids),
            role=claim.role,
            statement=claim.statement,
        )
        for claim in validated.claims
    )
    return RunAttributionOutput(
        case_id=gold.case_id,
        output_status=validated.output_status,
        attribution_type=validated.attribution_type,
        refusal_reason=validated.refusal_reason,
        refusal_reason_available=validated.refusal_reason_available,
        claims=claims,
        sanity_tasks_completed=tuple(validated.sanity_tasks_completed),
        latency_ms=validated.latency_ms,
        tokens=validated.tokens,
        cost_usd=validated.cost_usd,
        coverage_limited=False,
        model_limited=validated.model_limited,
    )

def load_output_audit(path: str | Path) -> list[Stage1OutputAudit]:
    """Load a JSONL human audit file (one sealed row per case)."""
    audits: list[Stage1OutputAudit] = []
    seen_cases: set[str] = set()
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            audit = Stage1OutputAudit.model_validate(row)
            if audit.case_id in seen_cases:
                raise ValueError(
                    f"{path}:{line_no}: duplicate audit row for case "
                    f"{audit.case_id!r}"
                )
            seen_cases.add(audit.case_id)
            claim_ids = [decision.claim_id for decision in audit.claims]
            if len(claim_ids) != len(set(claim_ids)):
                raise ValueError(
                    f"{path}:{line_no}: duplicate claim ids in audit for case "
                    f"{audit.case_id!r}"
                )
            audits.append(audit)
    if not audits:
        raise ValueError(f"{path}: audit file contains no rows")
    return audits


def validate_output_audit(
    audit: Stage1OutputAudit,
    run_output: Any,
    gold_case: GoldenCase,
    *,
    eval_id: str,
    run_manifest_id: str | None = None,
    run_manifest_hash: str | None = None,
    result_artifact_id: str | None = None,
    result_artifact_hash: str | None = None,
) -> bool:
    """Validate a sealed audit against the immutable run artifacts.

    Raises ValueError on any mismatch; returns True when the audit is
    complete and identity-bound. This function never authors human decisions.
    """
    if audit.schema_version != OUTPUT_AUDIT_SCHEMA_VERSION:
        raise ValueError("audit schema version mismatch")
    if audit.adjudication_state != "resolved":
        raise ValueError(
            f"audit adjudication_state must be 'resolved', got "
            f"{audit.adjudication_state!r}"
        )
    if audit.eval_id != eval_id:
        raise ValueError(
            f"audit eval_id mismatch: audit={audit.eval_id} expected={eval_id}"
        )
    if audit.case_id != gold_case.case_id:
        raise ValueError(
            f"audit case_id mismatch: audit={audit.case_id} "
            f"expected={gold_case.case_id}"
        )
    if run_manifest_id is not None and audit.run_manifest_id != run_manifest_id:
        raise ValueError("audit run_manifest_id does not match the sealed run")
    if run_manifest_hash is not None and audit.run_manifest_hash != run_manifest_hash:
        raise ValueError("audit run_manifest_hash does not match the sealed run")
    if result_artifact_id is not None and audit.result_artifact_id != result_artifact_id:
        raise ValueError("audit result_artifact_id does not match the sealed run")
    if result_artifact_hash is not None and audit.result_artifact_hash != result_artifact_hash:
        raise ValueError("audit result_artifact_hash does not match the sealed run")

    audited_ids = {decision.claim_id for decision in audit.claims}
    run_claims_by_id = {claim.claim_id: claim for claim in run_output.claims}
    for claim in run_output.claims:
        if claim.claim_id not in audited_ids:
            kind = "material claim" if claim.material else "claim"
            raise ValueError(
                f"{kind} {claim.claim_id!r} is missing from the human audit"
            )
    for decision in audit.claims:
        claim = run_claims_by_id.get(decision.claim_id)
        if claim is None:
            raise ValueError(
                f"audited claim {decision.claim_id!r} is not in the run output"
            )
        # Audit material/citation identities must exactly match the immutable
        # run output (Batch-B corrective): every cited unit is audited.
        if set(decision.citation_ids) != set(claim.citation_ids):
            raise ValueError(
                f"audit citation ids for claim {decision.claim_id!r} must "
                f"exactly match the run output: audit="
                f"{sorted(decision.citation_ids)} run={sorted(claim.citation_ids)}"
            )
        is_runtime_limitation = (
            claim.role == "LIMITATION"
            and claim.material is False
            and not claim.citation_ids
        )
        if is_runtime_limitation and (
            decision.decision != "SUPPORT"
            or decision.reason_code != "runtime_limitation_verified"
        ):
            raise ValueError(
                "non-material LIMITATION claims without citations require "
                "decision=SUPPORT and reason_code=runtime_limitation_verified"
            )
        if decision.reason_code == "runtime_limitation_verified" and not is_runtime_limitation:
            raise ValueError(
                "runtime_limitation_verified is legal only for non-material "
                "LIMITATION claims without citations"
            )
    return True


# New V1.1 public surface terminology: the sealed human output audit contract
# is ``HumanOutputAudit``. ``Stage1OutputAudit``/``OUTPUT_AUDIT_SCHEMA_VERSION``
# stay exported so sealed V1.1 artifacts and adapters keep validating.
HumanOutputAudit = Stage1OutputAudit


__all__ = [
    "validate_audit_against_ledger",
    "AuditClaimDecision",
    "AuditDecisionV1",
    "AuditReasonCodeV1",
    "OUTPUT_AUDIT_SCHEMA_VERSION",
    "supported_citation_ids_for",
    "HumanOutputAudit",
    "Stage1OutputAudit",
    "load_output_audit",
    "validate_output_audit",
]
