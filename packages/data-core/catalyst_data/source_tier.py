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
import sqlite3

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Publisher → tier mapping
# ---------------------------------------------------------------------------

_PUBLISHER_TIER: dict[str, int] = {
    "MarketWatch": 2,
    "GlobeNewswire": 3,
    "GlobeNewswire Inc.": 3,
    "Benzinga": 4,
    "Investing.com": 4,
    "The Motley Fool": 5,
    "Motley Fool": 5,
    "Zacks": 5,
    "Zacks Investment Research": 5,
    "Yahoo": 4,
    "Yahoo Finance": 4,
    "Yahoo Finance UK": 4,
    "Yahoo Finance Video": 4,
    "Seeking Alpha": 5,
    "Business Wire": 3,
    "PR Newswire": 3,
    "Accesswire": 4,
    "TipRanks": 5,
    "Investor's Business Daily": 4,
    "The Wall Street Journal": 2,
    "Reuters": 2,
    "Bloomberg": 2,
    "CNBC": 4,
    "Fox Business": 4,
    "Barrons": 4,
    "Morningstar": 4,
    "MarketBeat": 4,
    "24/7 Wall St.": 5,
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def tier_for_publisher(publisher_name: str | None) -> int:
    """Map a publisher name to a tier (1–6). Unknown → T4."""
    if publisher_name is None:
        return 4
    name = publisher_name.strip()
    if name in _PUBLISHER_TIER:
        return _PUBLISHER_TIER[name]
    return 4


def classify_articles(conn: sqlite3.Connection) -> int:
    """Update all articles WHERE source_tier IS NULL.

    Returns count of rows updated.  Idempotent — only touches NULLs.
    Unknown publishers are logged once per unique name (deduplicated via set).
    """
    # Discover unknown publisher names (log once each)
    known = list(_PUBLISHER_TIER.keys())
    placeholders = ",".join("?" for _ in known)
    rows = conn.execute(
        f"""SELECT DISTINCT publisher_name FROM articles
            WHERE source_tier IS NULL
              AND (publisher_name NOT IN ({placeholders}) OR publisher_name IS NULL)""",
        known,
    ).fetchall()
    for (name,) in rows:
        logger.warning("Unknown publisher: %s — assigning T4", name)

    # Build CASE expression to classify all NULLs in one UPDATE
    case_parts = []
    params: list[str] = []
    for publisher, tier in sorted(_PUBLISHER_TIER.items()):
        case_parts.append("WHEN publisher_name = ? THEN ?")
        params.extend([publisher, str(tier)])
    case_parts.append("ELSE 4")

    sql = f"""UPDATE articles SET source_tier = CASE {' '.join(case_parts)} END
              WHERE source_tier IS NULL"""
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


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
