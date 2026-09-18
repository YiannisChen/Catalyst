"""Attribution contract models and deterministic helpers for B5."""

from typing import Any

from catalyst_agents.attribution.provider import ContextInputs, ContextProvider, RetrievedEvidence, Retriever


def __getattr__(name: str) -> Any:
    """Load context-builder exports lazily to avoid manifest import cycles."""
    if name in {"ContextBuilder", "ContextBuilderArtifact", "canonical_context_bytes"}:
        from catalyst_agents.attribution import context_builder

        return getattr(context_builder, name)
    raise AttributeError(name)

__all__ = [
    "ContextBuilder",
    "ContextBuilderArtifact",
    "ContextInputs",
    "ContextProvider",
    "RetrievedEvidence",
    "Retriever",
    "canonical_context_bytes",
]
