from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal


MAX_LAYERS_P0 = 1
MAX_EXPANSIONS = 0
DEFAULT_TOP_K = 8
DEFAULT_CANDIDATE_DEPTH = 20


class Layer(str, Enum):
    DIRECT = "direct"
    MACRO = "macro"
    RELATED = "related"


@dataclass
class RetrievalMetadata:
    ticker: str
    trade_date: str
    date_range: tuple[str, str] | None = None
    cutoff: str | None = None
    db_path: Path | str | None = None
    lancedb_dir: Path | str | None = None
    table: Any = None
    embedding_fn: Any = None
    requested_manifest_id: str = "corpus-fixture-v1"
    retriever: Any = None
    top_k: int = DEFAULT_TOP_K
    candidate_depth: int = DEFAULT_CANDIDATE_DEPTH
    layers_attempted: list[Layer] = field(default_factory=list)
    expansion_reasons: list[str] = field(default_factory=list)
    stop_reason: Literal["sufficiency_reached", "expansions_exhausted", "layer3_not_implemented", "system_error"] | None = None
    hit_counts_per_layer: dict[Layer, int] = field(default_factory=dict)
    total_unique_evidence: int = 0


class RetrievalDependencyError(RuntimeError):
    pass


def check_sufficiency(chunks: list[dict[str, Any]], min_count: int = 1, min_mean_score: float = 0.0) -> bool:
    return len(chunks) >= min_count


def retrieve(query: str, layer: Layer, metadata: RetrievalMetadata, *, rerank: Any = None) -> list[Any]:
    if metadata.retriever is None:
        raise RetrievalDependencyError("B5 Miner requires an injected Retriever")
    metadata.layers_attempted.append(layer)
    if not metadata.expansion_reasons:
        metadata.expansion_reasons.append("initial")
    if metadata.cutoff is None:
        raise RetrievalDependencyError("B5 Miner requires a canonical cutoff")
    results = list(metadata.retriever.retrieve(
        query,
        ticker=metadata.ticker,
        cutoff=metadata.cutoff,
        requested_manifest_id=metadata.requested_manifest_id,
        top_k=metadata.top_k,
        candidate_depth=metadata.candidate_depth,
    ))
    metadata.hit_counts_per_layer[layer] = len(results)
    metadata.total_unique_evidence = len({getattr(item, "chunk_id", None) for item in results})
    metadata.stop_reason = "sufficiency_reached" if results else "expansions_exhausted"
    return results
