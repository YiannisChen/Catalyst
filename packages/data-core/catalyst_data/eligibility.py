"""Embed eligibility — per-association canonicality gate.

Defines the eligibility contract used by index_builder and by the S5 gate.
An article is embed-eligible iff:
  1. is_rag_eligible = 1 (quality-filtered articles are never eligible), AND
  2. It has >= 1 canonical article_tickers association.

This drops pure duplicate-losers but keeps singleton-winners.
"""

from __future__ import annotations

import sqlite3


def is_article_eligible(conn: sqlite3.Connection, article_id: str) -> bool:
    """True if this article should be embedded (index_state L1)."""
    row = conn.execute(
        """SELECT 1 FROM article_tickers at
           JOIN articles a ON a.article_id = at.article_id
           WHERE at.article_id = ?
             AND a.is_rag_eligible = 1
             AND at.is_canonical = 1
           LIMIT 1""",
        (article_id,),
    ).fetchone()
    return row is not None


def eligible_article_ids(conn: sqlite3.Connection) -> list[str]:
    """Return ordered list of all embed-eligible article IDs."""
    rows = conn.execute(
        """SELECT DISTINCT a.article_id FROM articles a
           JOIN article_tickers at ON at.article_id = a.article_id
           WHERE a.is_rag_eligible = 1
             AND at.is_canonical = 1
           ORDER BY a.article_id"""
    ).fetchall()
    return [r[0] for r in rows]


def eligible_article_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """SELECT COUNT(DISTINCT a.article_id) FROM articles a
           JOIN article_tickers at ON at.article_id = a.article_id
           WHERE a.is_rag_eligible = 1
             AND at.is_canonical = 1"""
    ).fetchone()
    return row[0] if row else 0
