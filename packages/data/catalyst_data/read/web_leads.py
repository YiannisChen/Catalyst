"""Pure issuer matching for web lead readers."""
from __future__ import annotations

import re
from typing import Callable

from catalyst_data.retrieval.web_bodies import subject_matcher

_CORPORATE_SUFFIXES = frozenset({"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc"})


def issuer_matcher(ticker: str, company_name: str | None = None) -> Callable[[str], bool]:
    """Web-lead issuer test: the shared subject matcher, plus the full company name as a phrase.

    The shared ``subject_matcher`` is used unchanged (the evaluated pipeline depends on it);
    this only widens it for web pages, which often name the issuer in full.
    """
    base = subject_matcher(ticker)
    tokens = [t for t in re.findall(r"[a-z0-9]+", (company_name or "").casefold()) if t not in _CORPORATE_SUFFIXES]
    phrase = (re.compile(r"(?<![A-Za-z0-9])" + r"\s+".join(map(re.escape, tokens)) + r"(?![A-Za-z0-9])", re.I)
              if tokens else None)
    return lambda text: bool(base(text)) or bool(phrase and phrase.search(text))
