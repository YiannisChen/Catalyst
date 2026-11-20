"""Data-owned assembly of the served lexical runtime binding."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from catalyst_data.canonical.identity import DataRuntimeIdentity
from catalyst_data.storage.connect import make_readonly_factory

from .lexical_display import RETRIEVAL_PROFILE, V1_QUERY_POLICY_VERSION
from .lexical_retriever import (
    LexicalDisplayRetriever,
    ServedBuildResolution,
    resolve_served_build,
)
from .result import RetrievalContractError
from .served_projection import ServedProjectionHandle, build_served_projection


@dataclass(frozen=True)
class LexicalRuntimeBinding:
    """Verified data objects needed to serve one lexical runtime."""

    retriever: LexicalDisplayRetriever
    data_runtime_identity: DataRuntimeIdentity
    resolution: ServedBuildResolution
    projection: ServedProjectionHandle


class LexicalRuntimeBindingError(RuntimeError):
    """A fail-closed binding error with a stable health-reporting phase."""

    def __init__(self, stage: str, message: str):
        self.stage = stage
        super().__init__(message)


def bind_lexical_runtime(
    *,
    sqlite_db_path: str | Path,
    expected_manifest_id: str | None,
    expected_fts_digest: str,
    data_snapshot_id: str,
    projection_dir_factory: Callable[[], str | Path],
) -> LexicalRuntimeBinding:
    """Resolve and construct a lexical retriever bound to the served tuple.

    Contract failures retain the historical stage-specific message prefixes
    through ``LexicalRuntimeBindingError.stage``. No retriever is returned
    unless the digest and projection both match the resolved served build.
    """
    readonly_factory = make_readonly_factory(sqlite_db_path)
    probe = readonly_factory()
    try:
        try:
            resolution = resolve_served_build(
                probe, expected_manifest_id=expected_manifest_id
            )
        except RetrievalContractError as exc:
            raise LexicalRuntimeBindingError("served-build resolution", str(exc)) from exc
    finally:
        probe.close()

    if expected_fts_digest != "served" and resolution.lexical_digest != expected_fts_digest:
        raise LexicalRuntimeBindingError(
            "digest-mismatch",
            "served FTS digest mismatch: the operator-supplied expected "
            f"digest {expected_fts_digest!r} does not match the live served "
            f"digest {resolution.lexical_digest!r}",
        )

    try:
        projection_dir = projection_dir_factory()
        projection = build_served_projection(
            readonly_factory,
            expected_manifest_id=resolution.corpus_manifest_id,
            projection_dir=projection_dir,
        )
    except (RetrievalContractError, OSError, sqlite3.Error) as exc:
        raise LexicalRuntimeBindingError("served projection", str(exc)) from exc

    if projection.build_id != resolution.build_id:
        raise LexicalRuntimeBindingError(
            "projection-build-mismatch",
            "served projection is not the resolved served build",
        )

    identity = DataRuntimeIdentity(
        data_snapshot_id=data_snapshot_id,
        corpus_manifest_id=resolution.corpus_manifest_id,
        fts_index_version=resolution.lexical_digest,
        dense_index_version=None,
        embedding_model_revision=None,
        reranker_revision=None,
        query_policy_version=V1_QUERY_POLICY_VERSION,
    )
    retriever = LexicalDisplayRetriever(
        db_conn_factory=readonly_factory,
        data_runtime_identity=identity,
        retrieval_policy_version=RETRIEVAL_PROFILE,
        served_projection=projection,
    )
    return LexicalRuntimeBinding(
        retriever=retriever,
        data_runtime_identity=identity,
        resolution=resolution,
        projection=projection,
    )


__all__ = [
    "LexicalRuntimeBinding",
    "LexicalRuntimeBindingError",
    "bind_lexical_runtime",
]
