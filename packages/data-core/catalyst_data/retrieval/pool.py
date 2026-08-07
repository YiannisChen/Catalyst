"""Persisted four-arm union judgment pool."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any

from .artifacts import load_arm_artifact


@dataclass(frozen=True)
class UnionJudgmentPool:
    schema_version: str
    case_id: str
    chunk_ids: tuple[str, ...]
    per_arm_chunk_ids: dict[str, tuple[str, ...]]
    corpus_manifest_id: str
    index_manifest_id: str
    source_artifact_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "chunk_ids": list(self.chunk_ids),
            "per_arm_chunk_ids": {
                name: list(values) for name, values in self.per_arm_chunk_ids.items()
            },
            "corpus_manifest_id": self.corpus_manifest_id,
            "index_manifest_id": self.index_manifest_id,
            "source_artifact_id": self.source_artifact_id,
        }


def _validate_pool(pool: UnionJudgmentPool) -> None:
    if pool.schema_version != "1.0.0" or not pool.case_id:
        raise ValueError("union pool schema or case identity mismatch")
    if re.fullmatch(r"[0-9a-f]{64}", pool.corpus_manifest_id) is None or re.fullmatch(r"[0-9a-f]{64}", pool.index_manifest_id) is None:
        raise ValueError("union pool manifest identity values must be bound SHA-256 values")
    if not re.fullmatch(r"[0-9a-f]{64}", pool.source_artifact_id):
        raise ValueError("union pool source identity is invalid")
    if set(pool.per_arm_chunk_ids) != {"fts5", "dense", "hybrid", "reranked"}:
        raise ValueError("union pool arm set mismatch")
    for name in ("fts5", "dense", "hybrid", "reranked"):
        values = pool.per_arm_chunk_ids[name]
        if not isinstance(values, (tuple, list)):
            raise ValueError(f"union pool per_arm_chunk_ids for {name} must be a sequence")
        seen: set[str] = set()
        for chunk_id in values:
            if not isinstance(chunk_id, str) or not chunk_id:
                raise ValueError(f"union pool chunk_id in {name} must be a non-empty string")
            if chunk_id in seen:
                raise ValueError(f"union pool duplicate chunk_id within {name}")
            seen.add(chunk_id)
    expected_union = tuple(dict.fromkeys(
        chunk_id
        for name in ("fts5", "dense", "hybrid", "reranked")
        for chunk_id in pool.per_arm_chunk_ids[name]
    ))
    if pool.chunk_ids != expected_union or any(not isinstance(chunk_id, str) or not chunk_id for chunk_id in pool.chunk_ids):
        raise ValueError("union pool chunk order or identity mismatch")


def generate_union_pool(artifact_path) -> UnionJudgmentPool:
    artifact = load_arm_artifact(artifact_path)
    per_arm = {
        name: tuple(item["chunk_id"] for item in arm["results"])
        for name, arm in artifact.arms.items()
    }
    union: list[str] = []
    seen: set[str] = set()
    for name in ("fts5", "dense", "hybrid", "reranked"):
        for chunk_id in per_arm[name]:
            if chunk_id not in seen:
                seen.add(chunk_id)
                union.append(chunk_id)
    filters = artifact.filters
    corpus_manifest_id = filters.get("corpus_manifest_id")
    index_manifest_id = filters.get("index_manifest_id")
    if not corpus_manifest_id or not index_manifest_id:
        raise ValueError("persisted arm artifact lacks manifest identities")
    return UnionJudgmentPool(
        schema_version="1.0.0",
        case_id=artifact.payload["case_id"],
        chunk_ids=tuple(union),
        per_arm_chunk_ids=per_arm,
        corpus_manifest_id=corpus_manifest_id,
        index_manifest_id=index_manifest_id,
        source_artifact_id=artifact.artifact_id,
    )


def write_union_pool(pool: UnionJudgmentPool, path: Path) -> Path:
    _validate_pool(pool)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(pool.to_dict(), handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    return destination


def load_union_pool(path: Path) -> UnionJudgmentPool:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "schema_version", "case_id", "chunk_ids", "per_arm_chunk_ids",
        "corpus_manifest_id", "index_manifest_id", "source_artifact_id",
    }
    if set(raw) != required or raw["schema_version"] != "1.0.0":
        raise ValueError("union pool schema mismatch")
    per_arm = raw["per_arm_chunk_ids"]
    if set(per_arm) != {"fts5", "dense", "hybrid", "reranked"}:
        raise ValueError("union pool arm set mismatch")
    for name in ("fts5", "dense", "hybrid", "reranked"):
        values = per_arm[name]
        if not isinstance(values, list):
            raise ValueError(f"union pool per_arm_chunk_ids for {name} must be a sequence")
    pool = UnionJudgmentPool(
        schema_version=raw["schema_version"], case_id=raw["case_id"],
        chunk_ids=tuple(raw["chunk_ids"]),
        per_arm_chunk_ids={name: tuple(values) for name, values in per_arm.items()},
        corpus_manifest_id=raw["corpus_manifest_id"],
        index_manifest_id=raw["index_manifest_id"],
        source_artifact_id=raw["source_artifact_id"],
    )
    _validate_pool(pool)
    return pool


__all__ = ["UnionJudgmentPool", "generate_union_pool", "load_union_pool", "write_union_pool"]
