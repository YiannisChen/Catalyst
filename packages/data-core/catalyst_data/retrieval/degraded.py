"""Deterministic SQL fallback for lexical retrieval."""

from __future__ import annotations

import sqlite3


def rank_sql_fallback(
    conn: sqlite3.Connection,
    *,
    eligibility_sql: str,
    eligibility_params: list[object],
    terms: tuple[str, ...],
    candidate_depth: int,
    chunks_relation: str = "corpus_chunks",
) -> tuple[list[tuple], int]:
    rows = conn.execute(
        f"""SELECT c.chunk_id, c.document_id, c.available_at, c.source_class,
                   c.content_text
            FROM {chunks_relation} c
            WHERE {eligibility_sql}
            ORDER BY c.chunk_id ASC""",
        eligibility_params,
    ).fetchall()
    scored: list[tuple[int, tuple]] = []
    for row in rows:
        content = row[4].casefold()
        if all(term in content for term in terms):
            scored.append((sum(content.count(term) for term in terms), row))
    scored.sort(key=lambda item: (-item[0], item[1][0]))
    return [row for _, row in scored[:candidate_depth]], len(scored)
