"""Retrieval policy exports for Catalyst agents.

The package keeps a lazy fallback so importing the subpackage never triggers
the policy module before its dependencies are ready (M4-2 circular-import
fix). Direct submodule imports remain the canonical path.
"""
from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    from catalyst_agents.retrieval import policy as _policy

    return getattr(_policy, name)


__all__ = [
    "InitialResearchPolicy",
    "Layer",
    "MAX_INITIAL_TASKS",
    "RetrievalMetadata",
    "ScenarioClassification",
    "check_sufficiency",
    "retrieve",
]
