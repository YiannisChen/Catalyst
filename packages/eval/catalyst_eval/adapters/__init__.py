"""Adapters from application runtime artifacts into evaluation schemas."""

from catalyst_eval.adapters.agents import make_catalyst_predict, make_rag_only_predict

__all__ = ["make_catalyst_predict", "make_rag_only_predict"]
