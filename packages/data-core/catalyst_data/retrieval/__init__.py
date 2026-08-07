"""Canonical retrieval contracts and lexical implementation."""

from .cutoff import CutoffPolicyError, ExchangeCutoffPolicy, compute_cutoff
from .fts5 import retrieve_lexical
from .fts5_builder import LexicalIndexBuildError, LexicalIndexBuildResult, build_fts5_index
from .result import (
    RetrievalArmUnavailableError,
    RetrievalContractError,
    RetrievalFilters,
    RetrievalResult,
    RetrievalResultSet,
)
from .trace import RetrievalTrace
from .dense import retrieve_dense
from .fusion import compute_rrf, fuse
from .hybrid import HybridRetrievalResult, ProductionHybridRetriever, retrieve_hybrid
from .index_manifest import IndexManifest
from .pool import UnionJudgmentPool, generate_union_pool, load_union_pool, write_union_pool
from .reranker import rerank

__all__ = [
    "CutoffPolicyError", "ExchangeCutoffPolicy", "LexicalIndexBuildError", "LexicalIndexBuildResult",
    "RetrievalContractError", "RetrievalArmUnavailableError", "RetrievalFilters", "RetrievalResult",
    "RetrievalResultSet", "RetrievalTrace", "build_fts5_index",
    "compute_cutoff", "retrieve_lexical",
    "retrieve_dense", "compute_rrf", "fuse", "HybridRetrievalResult",
    "retrieve_hybrid", "ProductionHybridRetriever", "IndexManifest", "UnionJudgmentPool",
    "generate_union_pool", "load_union_pool", "write_union_pool", "rerank",
]
