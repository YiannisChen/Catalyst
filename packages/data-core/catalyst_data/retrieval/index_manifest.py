"""Deterministic identity contract for the imported LanceDB dense index."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any

from catalyst_data.config import BGE_M3_MODEL, BGE_M3_REVISION
from catalyst_data.corpus.tokenizer import TOKENIZER_REVISION


_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_REQUIRED_ARTIFACTS = frozenset({"vectors.npy", "chunk_ids.json", "lancedb_table"})


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class IndexManifest:
    model_name: str
    model_revision: str
    tokenizer_revision: str
    normalization_mode: str
    dtype: str
    dimension: int
    corpus_manifest_id: str
    source_bundle_id: str
    snapshot_id: str
    probe_report_id: str
    postbuild_readiness_id: str
    artifact_hashes: dict[str, str]
    code_revision: str
    vector_count: int = 0
    chunk_order_checksum: str | None = None
    vectors_checksum: str | None = None
    artifact_state: str = "lancedb_imported"
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if self.model_name != BGE_M3_MODEL or self.model_revision != BGE_M3_REVISION:
            raise ValueError("model name and revision must use the pinned BGE-M3 contract")
        if self.tokenizer_revision != TOKENIZER_REVISION:
            raise ValueError("tokenizer revision must use the pinned contract")
        if self.normalization_mode != "l2":
            raise ValueError("dense index normalization_mode must be l2")
        if self.dtype != "float32" or self.dimension != 1024:
            raise ValueError("BGE-M3 index contract requires float32/1024")
        for name, value in {
            "corpus_manifest_id": self.corpus_manifest_id,
            "source_bundle_id": self.source_bundle_id,
            "snapshot_id": self.snapshot_id,
            "probe_report_id": self.probe_report_id,
            "postbuild_readiness_id": self.postbuild_readiness_id,
        }.items():
            if not _HEX64.fullmatch(value):
                raise ValueError(f"{name} must be lowercase SHA-256")
        if not _HEX40.fullmatch(self.code_revision):
            raise ValueError("code revision must be an exact 40-character Git revision")
        if self.vector_count < 0:
            raise ValueError("vector_count must be non-negative")
        if self.artifact_state not in {"vectors_staged", "lancedb_imported"}:
            raise ValueError("artifact_state must be vectors_staged or lancedb_imported")
        if not _REQUIRED_ARTIFACTS.issubset(self.artifact_hashes):
            missing = ",".join(sorted(_REQUIRED_ARTIFACTS - set(self.artifact_hashes)))
            raise ValueError(f"IndexManifest artifact hashes missing: {missing}")
        for name, value in self.artifact_hashes.items():
            if not isinstance(name, str) or not _HEX64.fullmatch(value):
                raise ValueError("artifact hashes must be lowercase SHA-256")

    def to_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema_version": self.schema_version,
            "model_name": self.model_name,
            "model_revision": self.model_revision,
            "tokenizer_revision": self.tokenizer_revision,
            "normalization_mode": self.normalization_mode,
            "dtype": self.dtype,
            "dimension": self.dimension,
            "corpus_manifest_id": self.corpus_manifest_id,
            "source_bundle_id": self.source_bundle_id,
            "snapshot_id": self.snapshot_id,
            "probe_report_id": self.probe_report_id,
            "postbuild_readiness_id": self.postbuild_readiness_id,
            "artifact_hashes": dict(sorted(self.artifact_hashes.items())),
            "code_revision": self.code_revision,
            "vector_count": self.vector_count,
            "chunk_order_checksum": self.chunk_order_checksum,
            "vectors_checksum": self.vectors_checksum,
            "artifact_state": self.artifact_state,
        }
        if include_id:
            body["index_manifest_id"] = hashlib.sha256(_canonical(body)).hexdigest()
        return body

    @property
    def index_manifest_id(self) -> str:
        return self.to_dict(include_id=True)["index_manifest_id"]

    @property
    def manifest_id(self) -> str:
        return self.index_manifest_id

    def assert_approved_identities(
        self,
        *,
        source_bundle_id: str,
        snapshot_id: str,
        corpus_manifest_id: str,
        probe_report_id: str,
        postbuild_readiness_id: str,
    ) -> None:
        expected = {
            "source_bundle_id": source_bundle_id,
            "snapshot_id": snapshot_id,
            "corpus_manifest_id": corpus_manifest_id,
            "probe_report_id": probe_report_id,
            "postbuild_readiness_id": postbuild_readiness_id,
        }
        actual = {
            "source_bundle_id": self.source_bundle_id,
            "snapshot_id": self.snapshot_id,
            "corpus_manifest_id": self.corpus_manifest_id,
            "probe_report_id": self.probe_report_id,
            "postbuild_readiness_id": self.postbuild_readiness_id,
        }
        mismatched = sorted(name for name in expected if expected[name] != actual[name])
        if mismatched:
            raise ValueError(
                "IndexManifest is not bound to the approved source identities: "
                + ",".join(mismatched)
            )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "IndexManifest":
        values = dict(raw)
        stored_id = values.pop("index_manifest_id", None)
        instance = cls(**values)
        if stored_id is not None and stored_id != instance.index_manifest_id:
            raise ValueError("index_manifest_id mismatch")
        return instance


__all__ = ["IndexManifest"]
