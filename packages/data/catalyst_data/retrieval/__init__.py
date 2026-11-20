"""Canonical retrieval contracts and lexical implementation.

Names are resolved lazily (PEP 562): importing ``catalyst_data.retrieval`` or
one of its submodules loads only what is used. The dense / fusion / hybrid /
rerank stack was retired in P5 (docs/ARCHIVE.md).
"""
from __future__ import annotations

import importlib

_EXPORTS = {
    "retrieve_lexical": ".fts5",
    "LexicalIndexBuildError": ".fts5_builder", "LexicalIndexBuildResult": ".fts5_builder",
    "build_fts5_index": ".fts5_builder",
    "RetrievalArmUnavailableError": ".result", "RetrievalContractError": ".result",
    "RetrievalFilters": ".result", "RetrievalResult": ".result", "RetrievalResultSet": ".result",
    "RetrievalTrace": ".trace",
    "DISPLAY_TOP_K": ".lexical_display", "LEXICAL_DISPLAY_POLICY_HASH": ".lexical_display",
    "RETRIEVAL_PROFILE": ".lexical_display", "V1_QUERY_POLICY_VERSION": ".lexical_display",
    "WithinDocumentSlot": ".lexical_display", "displayed_chunk_ids": ".lexical_display",
    "select_within_document_slots": ".lexical_display",
    "LexicalDisplayObservation": ".lexical_retriever", "LexicalDisplayRetriever": ".lexical_retriever",
    "ObservedLexicalDisplay": ".lexical_retriever", "ServedBuildResolution": ".lexical_retriever",
    "resolve_served_build": ".lexical_retriever",
    "LexicalRuntimeBinding": ".runtime_binding",
    "LexicalRuntimeBindingError": ".runtime_binding",
    "bind_lexical_runtime": ".runtime_binding",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
