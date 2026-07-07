"""Source tier classifier: maps publisher_name → tier 1-6 and populates articles.source_tier.

Tiers:
  T1 – Primary Source        (reserved: SEC filings — Step 3)
  T2 – Premium Financial Press (MarketWatch)
  T3 – Wire Service           (GlobeNewswire)
  T4 – Aggregator / Specialist (Benzinga, Investing.com; unknown fallback)
  T5 – Opinion / Retail Media (Motley Fool, Zacks)
  T6 – Macro / Data           (reserved: FRED)
"""

from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Publisher → tier mapping (case-insensitive keys — N1)
# ---------------------------------------------------------------------------

_PUBLISHER_TIER: dict[str, int] = {
    "marketwatch": 2,
    "globenewswire": 3,
    "globenewswire inc.": 3,
    "benzinga": 4,
    "investing.com": 4,
    "the motley fool": 5,
    "motley fool": 5,
    "zacks": 5,
    "zacks investment research": 5,
    "yahoo": 4,
    "yahoo finance": 4,
    "yahoo finance uk": 4,
    "yahoo finance video": 4,
    "seeking alpha": 5,
    "business wire": 3,
    "pr newswire": 3,
    "accesswire": 4,
    "tipranks": 5,
    "investor's business daily": 4,
    "the wall street journal": 2,
    "reuters": 2,
    "bloomberg": 2,
    "cnbc": 4,
    "fox business": 4,
    "barrons": 4,
    "morningstar": 4,
    "marketbeat": 4,
    "24/7 wall st.": 5,
}

# Spelling variants only (whitespace, abbreviations, punctuation differences).
# Case mirrors are NOT needed — _PUBLISHER_TIER lookup is case-insensitive.
_PUBLISHER_ALIASES: dict[str, str] = {
    "seekingalpha": "Seeking Alpha",
    "247 wall st.": "24/7 Wall St.",
    "247wallst": "24/7 Wall St.",
    "investors business daily": "Investor's Business Daily",
    "investorsbusinessdaily": "Investor's Business Daily",
    "wsj": "The Wall Street Journal",
}

_WHITESPACE_RE = re.compile(r"\s+")

# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


def canonicalize_publisher(publisher_name: str | None) -> str | None:
    """Normalize a publisher name for tier lookup.

    1. None/empty → None
    2. Strip leading/trailing whitespace
    3. Collapse all internal whitespace runs to a single ASCII space
    4. Unicode NFC normalization
    5. Lookup lowercase result in _PUBLISHER_ALIASES (also lowercase keys)
    6. If found → return alias value (canonical form)
    7. Else → return the NFC-normalized, whitespace-collapsed string
    """
    if publisher_name is None:
        return None
    name = publisher_name.strip()
    if not name:
        return None
    name = _WHITESPACE_RE.sub(" ", name)
    name = unicodedata.normalize("NFC", name)
    alias_key = name.lower()
    if alias_key in _PUBLISHER_ALIASES:
        return _PUBLISHER_ALIASES[alias_key]
    return name


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def tier_for_publisher(publisher_name: str | None) -> int:
    """Map a publisher name to a tier (1–6). Unknown → T4.

    Canonicalizes the name first (whitespace + alias resolution),
    then does case-insensitive lookup in _PUBLISHER_TIER.
    """
    canonical = canonicalize_publisher(publisher_name)
    if canonical is None:
        return 4
    lookup = canonical.lower()
    return _PUBLISHER_TIER.get(lookup, 4)


def classify_articles(conn: sqlite3.Connection) -> int:
    """Update all articles WHERE source_tier IS NULL.

    Returns count of rows updated.  Idempotent — only touches NULLs.
    Unknown publishers are logged once per unique canonical name.
    """
    rows = conn.execute(
        "SELECT article_id, publisher_name FROM articles WHERE source_tier IS NULL"
    ).fetchall()

    updated = 0
    seen_unknown: set[str] = set()
    for article_id, publisher_name in rows:
        tier = tier_for_publisher(publisher_name)
        canonical = canonicalize_publisher(publisher_name)
        if canonical and canonical.lower() not in _PUBLISHER_TIER:
            if canonical not in seen_unknown:
                logger.warning("Unknown publisher: %s — assigning T4", canonical)
                seen_unknown.add(canonical)
        conn.execute(
            "UPDATE articles SET source_tier = ? WHERE article_id = ?",
            (tier, article_id),
        )
        updated += 1

    conn.commit()
    return updated


def materialize_all_tiers(conn: sqlite3.Connection) -> tuple[int, set[str]]:
    """Run tier assignment over ALL articles (no WHERE clause).

    Returns (changed_count, unknown_publishers: set[str]).
    changed_count = rows where source_tier value actually changed.
    unknown_publishers = canonical names not in _PUBLISHER_TIER.
    """
    rows = conn.execute(
        "SELECT article_id, publisher_name, source_tier FROM articles"
    ).fetchall()

    changed = 0
    unknown_publishers: set[str] = set()
    seen_unknown: set[str] = set()

    for article_id, publisher_name, existing_tier in rows:
        tier = tier_for_publisher(publisher_name)
        canonical = canonicalize_publisher(publisher_name)
        if canonical and canonical.lower() not in _PUBLISHER_TIER:
            unknown_publishers.add(canonical)
            if canonical not in seen_unknown:
                logger.warning("Unknown publisher: %s — assigning T4", canonical)
                seen_unknown.add(canonical)
        if tier != (existing_tier or 4):
            changed += 1
        conn.execute(
            "UPDATE articles SET source_tier = ? WHERE article_id = ?",
            (tier, article_id),
        )

    conn.commit()
    return changed, unknown_publishers


def unknown_publisher_audit(conn: sqlite3.Connection) -> dict:
    """Audit all articles for unknown publishers.

    Returns dict with unknown_publishers list, count, and gate_passed bool.
    """
    rows = conn.execute(
        "SELECT DISTINCT publisher_name FROM articles WHERE publisher_name IS NOT NULL"
    ).fetchall()

    unknown_set: set[str] = set()
    for (publisher_name,) in rows:
        canonical = canonicalize_publisher(publisher_name)
        if canonical and canonical.lower() not in _PUBLISHER_TIER:
            unknown_set.add(canonical)

    unknown_list = sorted(unknown_set)
    return {
        "unknown_publishers": unknown_list,
        "count": len(unknown_list),
        "gate_passed": len(unknown_list) == 0,
    }


def tier_distribution(conn: sqlite3.Connection) -> dict[int, int]:
    """Return {tier: count} for all articles."""
    rows = conn.execute(
        "SELECT source_tier, COUNT(*) FROM articles GROUP BY 1 ORDER BY 1"
    ).fetchall()
    return {tier: count for tier, count in rows}


def tier_label(tier: int) -> str:
    """Human-readable label for a tier number."""
    return {
        1: "T1 – Primary Source",
        2: "T2 – Premium Financial Press",
        3: "T3 – Wire Service",
        4: "T4 – Aggregator / Specialist",
        5: "T5 – Opinion / Retail Media",
        6: "T6 – Macro / Data",
    }.get(tier, f"T{tier} – Unknown")
