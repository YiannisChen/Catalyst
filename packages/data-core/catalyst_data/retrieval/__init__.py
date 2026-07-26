"""Canonical retrieval contracts and lexical implementation."""

from .cutoff import CutoffPolicyError, ExchangeCutoffPolicy, compute_cutoff
from .fts5 import retrieve_lexical
from .fts5_builder import LexicalIndexBuildError, LexicalIndexBuildResult, build_fts5_index
from .result import RetrievalContractError, RetrievalFilters, RetrievalResult, RetrievalResultSet
from .trace import RetrievalTrace

__all__ = [
    "CutoffPolicyError", "ExchangeCutoffPolicy", "LexicalIndexBuildError", "LexicalIndexBuildResult",
    "RetrievalContractError", "RetrievalFilters", "RetrievalResult",
    "RetrievalResultSet", "RetrievalTrace", "build_fts5_index",
    "compute_cutoff", "retrieve_lexical",
]
