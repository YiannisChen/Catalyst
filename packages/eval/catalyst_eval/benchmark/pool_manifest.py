"""PoolManifest schema — snapshot of retrieval pool for a benchmark case."""
from __future__ import annotations

import datetime
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class PoolArm(BaseModel):
    """One retrieval arm in a pool manifest."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    arm: Literal["lexical", "dense", "hybrid", "reranked"]
    version: str
    top_k: int

    @field_validator("top_k")
    @classmethod
    def _top_k_range(cls, v: int) -> int:
        if not 1 <= v <= 100:
            raise ValueError("top_k must be between 1 and 100")
        return v

    @field_validator("version")
    @classmethod
    def _semver(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", v):
            raise ValueError(f"Not a semantic version: {v}")
        return v


class PoolManifest(BaseModel):
    """A versioned snapshot of the retrieval pool for one benchmark case."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str
    pool_id: str
    case_id: str
    arms: tuple[PoolArm, ...]
    chunk_inventory: tuple[str, ...]
    corpus_manifest_id: str
    index_manifest_id: str | None
    source_artifact_id: str
    created_at: datetime.datetime

    @field_validator("case_id")
    @classmethod
    def _non_empty_case_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("case_id must be non-empty")
        return v.strip()

    @field_validator("schema_version")
    @classmethod
    def _semver(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", v):
            raise ValueError(f"Not a semantic version: {v}")
        return v

    @field_validator("created_at")
    @classmethod
    def _utc_aware(cls, v: datetime.datetime) -> datetime.datetime:
        if v.tzinfo is None or v.utcoffset() != datetime.timedelta(0):
            raise ValueError("created_at must use UTC offset +00:00")
        return v

    @field_validator("pool_id", "corpus_manifest_id", "source_artifact_id")
    @classmethod
    def _required_hashes(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("identity must be lowercase SHA-256")
        return value

    @field_validator("index_manifest_id")
    @classmethod
    def _optional_hash(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("index_manifest_id must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _validate_pool_id(self) -> "PoolManifest":
        arm_order = {"lexical": 0, "dense": 1, "hybrid": 2, "reranked": 3}
        if not self.arms or tuple(arm_order[arm.arm] for arm in self.arms) != tuple(sorted({arm_order[arm.arm] for arm in self.arms})):
            raise ValueError("arms must be non-empty, unique, and in canonical order")
        if self.chunk_inventory != tuple(sorted(set(self.chunk_inventory))):
            raise ValueError("chunk_inventory must be sorted and unique")
        if self.pool_id != self.compute_pool_id():
            raise ValueError(
                f"pool_id mismatch: expected {self.compute_pool_id()}, got {self.pool_id}"
            )
        return self

    def compute_pool_id(self) -> str:
        hash_input = {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "arms": [
                {"arm": a.arm, "version": a.version, "top_k": a.top_k}
                for a in self.arms
            ],
            "chunk_inventory": list(self.chunk_inventory),
            "corpus_manifest_id": self.corpus_manifest_id,
            "index_manifest_id": self.index_manifest_id,
            "source_artifact_id": self.source_artifact_id,
        }
        return hashlib.sha256(
            json.dumps(hash_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
