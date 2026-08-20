"""V1.1 RunManifest contract (M2-7).

Agents-owned production run identity (Final Migration TSD §14; Frozen §6.7).
RunManifest references DataRuntimeIdentity by ref/hash and never duplicates an
independently recomputed data identity.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from catalyst_data.canonical.temporal import TemporalIdentity

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class RunManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    request_hash: str
    temporal_identity: TemporalIdentity
    data_runtime_identity_ref: str
    data_runtime_identity_hash: str
    code_revision: str
    workflow_version: str
    policy_version: str
    analyst_model_id: str
    analyst_prompt_hash: str
    writer_model_id: str
    writer_prompt_hash: str
    context_pack_schema_version: str
    packing_policy_version: str
    context_token_budget: int
    tokenizer_policy: str
    hypothesis_schema_version: str
    claim_schema_version: str
    max_corrective_rounds: int = 1
    max_actions_per_batch: int = 1
    run_timeout_seconds: int = 60
    provider_capability_revision: str
    runtime_configuration: dict[str, Any] = {}

    @field_validator(
        "request_hash",
        "data_runtime_identity_hash",
        "analyst_prompt_hash",
        "writer_prompt_hash",
    )
    @classmethod
    def _sha256_hex(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value


__all__ = ["RunManifest"]
