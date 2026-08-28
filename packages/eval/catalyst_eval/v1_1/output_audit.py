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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from catalyst_eval.v1_1.case import GoldenCase

OUTPUT_AUDIT_SCHEMA_VERSION = "v1_1_stage1_output_audit_v1"
_SHA256_RE = __import__("re").compile(r"[0-9a-f]{64}\Z")


class AuditClaimDecision(BaseModel):
    """One audited claim decision; never generated or inferred by code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    material: bool
    citation_ids: tuple[str, ...] = ()
    decision: str
    reason_code: str


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


def load_output_audit(path: str | Path) -> list[Stage1OutputAudit]:
    """Load a JSONL human audit file (one sealed row per case)."""
    audits: list[Stage1OutputAudit] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            audits.append(Stage1OutputAudit.model_validate(row))
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
    for claim in run_output.claims:
        if claim.material and claim.claim_id not in audited_ids:
            raise ValueError(
                f"material claim {claim.claim_id!r} is missing from the human audit"
            )
    for decision in audit.claims:
        if decision.claim_id not in {claim.claim_id for claim in run_output.claims}:
            raise ValueError(
                f"audited claim {decision.claim_id!r} is not in the run output"
            )
    return True


__all__ = [
    "AuditClaimDecision",
    "OUTPUT_AUDIT_SCHEMA_VERSION",
    "Stage1OutputAudit",
    "load_output_audit",
    "validate_output_audit",
]
