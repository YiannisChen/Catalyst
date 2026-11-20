"""Shared constants and small helpers for quote attribution rules."""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo

from catalyst_data.retrieval.web_bodies import subject_matcher

_PRIMARY_SOURCE_CLASSES = {
    "sec", "issuer_disclosure", "corporate_press_release", "official_government",
}
_DAY_WORDS = ("monday", "tuesday", "wednesday", "thursday", "friday")
_ET = ZoneInfo("America/New_York")

_ISSUER_NAME_ALIASES = {
    "V": ("Visa",),
    "CMCSA": ("Comcast",),
    "CAT": ("Caterpillar",),
    "BA": ("Boeing",),
}


def _issuer_text_matches(text: str, ticker: str) -> bool:
    if subject_matcher(ticker)(text):
        return True
    return any(
        re.search(r"\b" + re.escape(alias) + r"\b", text, re.I)
        for alias in _ISSUER_NAME_ALIASES.get(ticker.upper(), ())
    )
