from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import sqlite3

from catalyst_data.config import CROSS_SOURCE_PRIORITY
from catalyst_data.quality import normalize_title

_TRACKING_PREFIXES = ("utm_", "ref_", "mc_")


@dataclass(frozen=True)
class AssetCandidate:
    source_type: str
    title: str
    published_utc: datetime
    url: str
    ticker_primary: str
    body_md: str = ""


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname.lower() if parsed.hostname else ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    netloc = host + port

    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith(_TRACKING_PREFIXES)
    ]
    query = urlencode(sorted(query_items, key=lambda item: (item[0], item[1])))

    path = parsed.path
    if path == "/":
        path = ""
    elif path.endswith("/") and path.count("/") == 2:
        path = path.rstrip("/")

    return urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))


def duplicate_across_source(a: AssetCandidate, b: AssetCandidate) -> bool:
    if canonical_url(a.url) == canonical_url(b.url):
        return True

    title_a = normalize_title(a.title)
    title_b = normalize_title(b.title)
    if title_a is None or title_b is None:
        return False

    return (
        title_a == title_b
        and a.ticker_primary == b.ticker_primary
        and abs((a.published_utc - b.published_utc).total_seconds()) < 14400
    )


def _priority_rank(source_type: str) -> int:
    try:
        return CROSS_SOURCE_PRIORITY.index(source_type)
    except ValueError:
        return len(CROSS_SOURCE_PRIORITY)


def select_canonical(duplicates: list[AssetCandidate]) -> AssetCandidate:
    if not duplicates:
        raise ValueError("duplicates must not be empty")

    return min(
        duplicates,
        key=lambda candidate: (
            _priority_rank(candidate.source_type),
            candidate.published_utc,
            -len(candidate.body_md),
            -int(bool(candidate.body_md.strip())),
        ),
    )

def compute_cross_source_dedup(conn: sqlite3.Connection) -> int:
    """Two-pass cross-source dedup: assign dedup_group_id, then recompute is_canonical.

    Pass 1: For every article WHERE dedup_group_id IS NULL AND provider IN
    ('polygon', 'finnhub'), compute a fingerprint per article_tickers association:
        SHA256(NFC(title.lower()) | ticker | reference_date)[:16]
    Sets articles.dedup_group_id and article_tickers.dedup_group_id.

    Pass 2: For EVERY dedup_group_id that has >=2 member articles, recompute
    is_canonical across ALL rows in that group via select_canonical(). This is
    NOT NULL-gated — a group that previously had one singleton and later gains
    a member must have its canonical recomputed over both.

    Returns count of dedup groups resolved.

    Idempotent — safe to re-run. Pass 1 only touches NULL dedup_group_id rows.
    Pass 2 updates all groups with >=2 members regardless of prior state.
    """
    import hashlib
    import json
    import unicodedata
    from catalyst_data.storage.sqlite import _migrate_article_tickers_dedup

    _migrate_article_tickers_dedup(conn)

    # ── Pass 1: Assign dedup_group_id per article_tickers association ──
    rows = conn.execute("""
        SELECT a.article_id, a.title, at.ticker, at.reference_date
        FROM articles a
        JOIN article_tickers at ON at.article_id = a.article_id
        WHERE a.dedup_group_id IS NULL
          AND a.provider IN ('polygon', 'finnhub')
    """).fetchall()

    fingerprint_map: dict[str, str] = {}  # (article_id, ticker) -> dedup_group_id
    for article_id, title, ticker, ref_date in rows:
        if not title or not ticker or not ref_date:
            continue
        title_norm = unicodedata.normalize("NFC", title.strip().lower())
        fp = hashlib.sha256(
            f"{title_norm}|{ticker}|{ref_date}".encode()
        ).hexdigest()[:16]
        fingerprint_map[(article_id, ticker)] = fp

    # Update article_tickers.dedup_group_id
    for (article_id, ticker), fp in fingerprint_map.items():
        conn.execute(
            "UPDATE article_tickers SET dedup_group_id = ? "
            "WHERE article_id = ? AND ticker = ?",
            (fp, article_id, ticker),
        )

    # Set articles.dedup_group_id to the lexicographically first ticker's fingerprint
    article_fps: dict[str, set[str]] = {}
    for (article_id, ticker), fp in fingerprint_map.items():
        article_fps.setdefault(article_id, set()).add(fp)
    for article_id, fps in article_fps.items():
        first_fp = sorted(fps)[0]
        conn.execute(
            "UPDATE articles SET dedup_group_id = ? WHERE article_id = ?",
            (first_fp, article_id),
        )

    conn.commit()

    # ── Pass 2: Recompute is_canonical for EVERY group with >=2 members ──
    # Find all groups with >=2 distinct articles (via article_tickers.dedup_group_id)
    groups = conn.execute("""
        SELECT dedup_group_id, COUNT(DISTINCT article_id) as cnt
        FROM article_tickers
        WHERE dedup_group_id IS NOT NULL
        GROUP BY dedup_group_id
        HAVING cnt >= 2
    """).fetchall()

    winners: set[str] = set()
    grouped_article_ids: set[str] = set()

    for (group_id, _) in groups:
        # Get all article rows in this group
        article_rows = conn.execute("""
            SELECT DISTINCT a.article_id, a.source_type, a.title, a.published_utc,
                   a.article_url, a.ticker, a.description
            FROM articles a
            JOIN article_tickers at ON at.article_id = a.article_id
            WHERE at.dedup_group_id = ?
        """, (group_id,)).fetchall()

        if len(article_rows) < 2:
            continue

        # Build AssetCandidate list
        candidates = []
        for (aid, source_type, title, pub_utc, url, ticker, desc) in article_rows:
            try:
                pub_dt = datetime.fromisoformat(
                    (pub_utc or "").replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pub_dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
            candidates.append(AssetCandidate(
                source_type=source_type or "",
                title=title or "",
                published_utc=pub_dt,
                url=url or "",
                ticker_primary=ticker or "",
                body_md=desc or "",
            ))

        # Select canonical
        try:
            winner = select_canonical(candidates)
        except (ValueError, IndexError):
            continue

        # Find the winning article_id
        winner_id = None
        for candidate, (aid, *_) in zip(candidates, article_rows):
            if (candidate.source_type == winner.source_type
                and candidate.title == winner.title
                and candidate.ticker_primary == winner.ticker_primary):
                winner_id = aid
                break

        if winner_id is None:
            continue

        winners.add(winner_id)
        for (aid, *_) in article_rows:
            grouped_article_ids.add(aid)

    # OR semantics: an article that wins ANY group is canonical;
    # an article that loses ALL its groups is 0.
    if grouped_article_ids:
        placeholders = ",".join("?" for _ in grouped_article_ids)
        conn.execute(
            f"UPDATE articles SET is_canonical = 0 WHERE article_id IN ({placeholders})",
            tuple(grouped_article_ids),
        )
    if winners:
        placeholders = ",".join("?" for _ in winners)
        conn.execute(
            f"UPDATE articles SET is_canonical = 1 WHERE article_id IN ({placeholders})",
            tuple(winners),
        )

    groups_resolved = len(winners)
    conn.commit()
    return groups_resolved
