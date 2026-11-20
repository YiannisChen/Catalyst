"""Session lead terms: what the day's headlines say, as query terms.

Headlines are leads, never evidence (ADR-011 §2.5). Their one job here is to
steer retrieval toward the full-text primary source they point at: a day of
"Medicare Advantage rates" headlines should query CMS text, a day of "tariff
China Geneva" headlines should query the trade statement.

Deterministic: headlines associated with the ticker inside the session
information window, tokenized, stop-listed, ranked by how many headlines use
the term.
"""
from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta

LEAD_TERMS_POLICY = "session_headline_terms_v2"
MAX_LEAD_TERMS = 5
MIN_HEADLINES_PER_TERM = 2
BASELINE_DAYS = 30
# Roundup headlines ("5 stocks moving today") name many tickers; they say
# little about any one of them.
MAX_TICKERS_PER_HEADLINE = 3

_TOKEN = re.compile(r"[a-z][a-z0-9\-]{2,}")
_STOP = frozenset(
    """
    the and for with its are why how what who when where this that these those
    from after amid over into about than then they their them you your our not
    but all any can could should would will may might has have had was were been
    being also just now new more most less top here there still ahead again
    stock stocks share shares today market markets news investors says said
    week day days monday tuesday wednesday thursday friday saturday sunday
    january february march april may june july august september october
    november december year years inc corp too why
    buy sell hold watch rating price target maintains raises lowers upgrade
    downgrade analyst analysts moving movers higher lower rally rallies
    jumping jumps jumped soaring soars soared skyrocketing skyrockets
    skyrocketed surging surges surged plunging plunges plunged plummeted
    plummets sinking sinks sank tumbling tumbles tumbled falling falls fell
    rising rises rose climbing climbs climbed gains losses dropping drops
    """.split()
)


def _titles(conn, ticker: str, start: str, end: str) -> set[str]:
    rows = conn.execute(
        "SELECT a.title, (SELECT count(*) FROM canonical_asset_tickers x "
        "WHERE x.asset_id = a.asset_id) FROM canonical_asset_tickers t "
        "JOIN canonical_assets a ON a.asset_id = t.asset_id "
        "WHERE t.ticker = ? AND a.eligible_at > ? AND a.eligible_at <= ? "
        "AND a.title IS NOT NULL",
        (ticker, start, end),
    ).fetchall()
    return {
        str(title).strip()
        for title, tickers in rows
        if title and tickers <= MAX_TICKERS_PER_HEADLINE
    }


def _frequency(titles: set[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for title in titles:
        counts.update(
            token for token in set(_TOKEN.findall(title.lower())) if token not in _STOP
        )
    return counts


def session_lead_terms(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    window_start_at: str,
    cutoff_at: str,
    max_terms: int = MAX_LEAD_TERMS,
) -> tuple[str, ...]:
    """Terms unusually frequent in ``ticker``'s headlines for the session.

    Score = headlines using the term today x log(baseline headlines / baseline
    headlines using it). The company's own name is in most baseline headlines,
    so it scores near zero without a name list.
    """
    today = _titles(conn, ticker, window_start_at, cutoff_at)
    if len(today) < MIN_HEADLINES_PER_TERM:
        return ()
    start = datetime.fromisoformat(window_start_at.replace("Z", "+00:00"))
    baseline_start = (start - timedelta(days=BASELINE_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    baseline = _titles(conn, ticker, baseline_start, window_start_at)
    base_counts = _frequency(baseline)
    total = len(baseline) + 1
    scored = [
        (count * math.log(total / (base_counts.get(term, 0) + 1)), term)
        for term, count in _frequency(today).items()
        if count >= MIN_HEADLINES_PER_TERM and term != ticker.lower()
    ]
    ranked = sorted((item for item in scored if item[0] > 0), key=lambda i: (-i[0], i[1]))
    return tuple(term for _, term in ranked[:max_terms])


__all__ = ["LEAD_TERMS_POLICY", "session_lead_terms"]
