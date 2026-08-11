"""Atomic, identity-stable retrieval-arm artifact persistence."""

from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from catalyst_data.config import BGE_M3_REVISION, BGE_RERANKER_REVISION


class ArtifactValidationError(ValueError):
    pass


_ARM_NAMES = ("fts5", "dense", "hybrid", "reranked")
_MODE_SERVING = {"fts5", "dense", "hybrid", "reranked", "sql_like", "failed"}
_TOP_LEVEL_KEYS = {
    "schema_version", "artifact_id", "run_id", "case_id", "query_sha256",
    "cutoff_ts", "filters", "retrieval_config", "arms", "created_at",
}
_FILTER_KEYS = {"ticker", "evidence_types", "source_classes", "corpus_manifest_id", "index_manifest_id"}
_CONFIG_KEYS = {
    "lexical_top_k", "dense_top_k", "fusion_k", "fused_top_k", "display_top_k",
    "embedding_revision", "reranker_revision", "reranker_timeout_seconds",
}
_ARM_KEYS = {"mode_requested", "mode_served", "status", "latency_ms", "degradation_reasons", "results"}
_RESULT_KEYS = {
    "chunk_id", "document_id", "available_at", "source_class", "rank",
    "lexical_raw_score", "lexical_rank", "dense_score", "dense_rank",
    "fusion_score", "fusion_rank", "arm_ranks", "arm_scores",
    "reranker_score", "reranker_rank",
}


def _without_runtime_fields(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            name: _without_runtime_fields(item, key=name)
            for name, item in value.items()
            if name not in {"artifact_id", "created_at", "latency_ms"}
        }
    if isinstance(value, list):
        return [_without_runtime_fields(item) for item in value]
    return value


def compute_arm_artifact_id(payload: Any) -> str:
    if hasattr(payload, "to_dict"):
        payload = payload.to_dict()
    semantic = _without_runtime_fields(copy.deepcopy(payload))
    return hashlib.sha256(
        json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validate_payload(payload: dict[str, Any]) -> None:
    if set(payload) != _TOP_LEVEL_KEYS:
        raise ArtifactValidationError("top-level artifact fields mismatch")
    if payload.get("schema_version") != "1.0.0":
        raise ArtifactValidationError("schema_version mismatch")
    if not isinstance(payload.get("run_id"), str) or not payload["run_id"]:
        raise ArtifactValidationError("run_id is required")
    if not isinstance(payload.get("case_id"), str) or not payload["case_id"]:
        raise ArtifactValidationError("case_id is required")
    if not isinstance(payload.get("query_sha256"), str) or len(payload["query_sha256"]) != 64:
        raise ArtifactValidationError("query_sha256 is required")
    filters = payload.get("filters")
    if not isinstance(filters, dict) or set(filters) != _FILTER_KEYS:
        raise ArtifactValidationError("filters fields mismatch")
    if not isinstance(filters["ticker"], str) or not filters["ticker"]:
        raise ArtifactValidationError("filters ticker is required")
    for key in ("evidence_types", "source_classes"):
        if not isinstance(filters[key], list) or any(not isinstance(value, str) for value in filters[key]):
            raise ArtifactValidationError(f"filters {key} is invalid")
    for key in ("corpus_manifest_id", "index_manifest_id"):
        if not isinstance(filters[key], str) or re.fullmatch(r"[0-9a-f]{64}", filters[key]) is None:
            raise ArtifactValidationError(f"filters {key} must be a bound SHA-256 identity")
    config = payload.get("retrieval_config")
    if not isinstance(config, dict) or set(config) != _CONFIG_KEYS:
        raise ArtifactValidationError("retrieval_config fields mismatch")
    if {key: config[key] for key in ("lexical_top_k", "dense_top_k", "fusion_k", "fused_top_k", "display_top_k")} != {
        "lexical_top_k": 20, "dense_top_k": 20, "fusion_k": 60, "fused_top_k": 20, "display_top_k": 8,
    }:
        raise ArtifactValidationError("retrieval_config top-k contract mismatch")
    if config["embedding_revision"] != BGE_M3_REVISION or config["reranker_revision"] != BGE_RERANKER_REVISION:
        raise ArtifactValidationError("retrieval_config revisions are not pinned")
    arms = payload.get("arms")
    if not isinstance(arms, dict) or set(arms) != set(_ARM_NAMES):
        raise ArtifactValidationError("all four retrieval arms are required")
    for name in _ARM_NAMES:
        arm = arms[name]
        if not isinstance(arm, dict) or set(arm) != _ARM_KEYS:
            raise ArtifactValidationError(f"arm fields mismatch for {name}")
        if arm.get("mode_requested") != name:
            raise ArtifactValidationError(f"mode_requested mismatch for {name}")
        if arm.get("mode_served") not in _MODE_SERVING:
            raise ArtifactValidationError(f"invalid mode_served for {name}")
        if arm.get("status") not in {"ok", "failed"}:
            raise ArtifactValidationError(f"invalid status for {name}")
        if not isinstance(arm.get("latency_ms"), (int, float)) or arm["latency_ms"] < 0:
            raise ArtifactValidationError(f"invalid latency for {name}")
        if not isinstance(arm.get("degradation_reasons"), list) or any(
            not isinstance(reason, str) for reason in arm["degradation_reasons"]
        ):
            raise ArtifactValidationError(f"invalid degradation_reasons for {name}")
        results = arm.get("results")
        if not isinstance(results, list):
            raise ArtifactValidationError(f"results missing for {name}")
        seen: set[str] = set()
        for position, result in enumerate(results, start=1):
            if not isinstance(result, dict):
                raise ArtifactValidationError(f"result fields mismatch in {name}")
            if "rank" not in result:
                raise ArtifactValidationError(f"rank is required in {name}")
            if type(result["rank"]) is not int:
                raise ArtifactValidationError(f"rank must be an integer in {name}")
            if set(result) != _RESULT_KEYS:
                raise ArtifactValidationError(f"result fields mismatch in {name}")
            chunk_id = result.get("chunk_id")
            if not isinstance(chunk_id, str) or not chunk_id or chunk_id in seen:
                raise ArtifactValidationError(f"duplicate or missing chunk_id in {name}")
            if result["rank"] != position:
                raise ArtifactValidationError(f"result order/rank mismatch in {name}")
            seen.add(chunk_id)
    expected = compute_arm_artifact_id(payload)
    if payload.get("artifact_id") not in (None, "", expected):
        raise ArtifactValidationError("artifact_id mismatch")


@dataclass(frozen=True)
class ArmArtifact:
    payload: dict[str, Any]

    @property
    def schema_version(self) -> str:
        return self.payload["schema_version"]

    @property
    def artifact_id(self) -> str:
        return self.payload["artifact_id"]

    @property
    def arms(self) -> dict[str, Any]:
        return self.payload["arms"]

    @property
    def filters(self) -> dict[str, Any]:
        return self.payload.get("filters", {})

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.payload)


def write_arm_artifact(
    *,
    root: Path,
    run_id: str,
    case_id: str,
    query: str,
    cutoff_ts: str,
    filters: dict[str, Any],
    retrieval_config: dict[str, Any],
    arms: dict[str, Any],
    created_at: str = "1970-01-01T00:00:00Z",
) -> Path:
    payload = {
        "schema_version": "1.0.0",
        "artifact_id": "",
        "run_id": run_id,
        "case_id": case_id,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "cutoff_ts": cutoff_ts,
        "filters": copy.deepcopy(filters),
        "retrieval_config": copy.deepcopy(retrieval_config),
        "arms": copy.deepcopy(arms),
        "created_at": created_at,
    }
    _validate_payload(payload)
    payload["artifact_id"] = compute_arm_artifact_id(payload)
    destination = Path(root) / run_id / f"{case_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    return destination


def load_arm_artifact(path: Path) -> ArmArtifact:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate_payload(payload)
    payload["arms"] = {name: payload["arms"][name] for name in _ARM_NAMES}
    if payload.get("artifact_id") != compute_arm_artifact_id(payload):
        raise ArtifactValidationError("artifact_id mismatch")
    return ArmArtifact(payload)


__all__ = ["ArmArtifact", "ArtifactValidationError", "compute_arm_artifact_id", "load_arm_artifact", "write_arm_artifact"]
