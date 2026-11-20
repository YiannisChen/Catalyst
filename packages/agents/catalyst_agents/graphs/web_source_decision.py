"""Issuer-specific filtering shared by graph and cascade paths."""

from __future__ import annotations

from typing import Any


def web_source_decision(decision: Any, *, title: str, ticker: str, company_name: str) -> Any:
    """Keep only company-news PRIMARYs that name the issuer in title and quote."""
    from catalyst_data.read.web_leads import issuer_matcher

    names_issuer = issuer_matcher(ticker, company_name)

    kept = []
    for hypothesis in decision.candidate_hypotheses:
        role = getattr(getattr(hypothesis, "proposed_role", None), "value", getattr(hypothesis, "proposed_role", None))
        cause = getattr(getattr(hypothesis, "cause_type", None), "value", getattr(hypothesis, "cause_type", None))
        quotes = tuple(getattr(hypothesis, "quotes", ()))
        if role != "PRIMARY" or cause != "COMPANY_SPECIFIC_CATALYST" or not names_issuer(title):
            continue
        issuer_quotes = tuple(
            quote for quote in quotes if names_issuer(str(getattr(quote, "text", "")))
        )
        if not issuer_quotes:
            continue
        kept.append(hypothesis.model_copy(update={"quotes": issuer_quotes}))
    return decision.model_copy(update={"candidate_hypotheses": tuple(kept)})
