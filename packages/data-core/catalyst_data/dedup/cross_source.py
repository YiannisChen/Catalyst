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


def recompute_per_association_canonical(conn: sqlite3.Connection) -> dict:
    """Recompute article_tickers.is_canonical: exactly 1 canonical association per (group, ticker).

    Singleton groups (1 article, 1 association): that association is canonical.
    Multi-article groups: select_canonical winner per ticker is canonical;
    losing associations are non-canonical.

    Also recomputes articles.is_canonical as derived: 1 if ANY association is canonical.

    Returns dict with before/after counts for observability.
    Idempotent — safe to re-run.
    """
    from catalyst_data.storage.sqlite import _migrate_article_tickers_dedup
    _migrate_article_tickers_dedup(conn)

    before_ats = conn.execute(
        "SELECT COUNT(*) FROM article_tickers WHERE dedup_group_id IS NOT NULL"
    ).fetchone()[0]

    # Reset all article_tickers.is_canonical to 0 for rows with dedup_group_id
    conn.execute(
        "UPDATE article_tickers SET is_canonical = 0 WHERE dedup_group_id IS NOT NULL"
    )
    conn.commit()

    # Find all dedup groups and assign one canonical per (group, ticker)
    groups = conn.execute("""
        SELECT at.dedup_group_id, at.ticker, COUNT(DISTINCT at.article_id) as article_cnt
        FROM article_tickers at
        WHERE at.dedup_group_id IS NOT NULL
        GROUP BY at.dedup_group_id, at.ticker
    """).fetchall()

    winners_set: set[tuple[str, str]] = set()  # (article_id, ticker)
    canonical_count = 0

    for group_id, ticker, article_cnt in groups:
        if article_cnt == 1:
            # Singleton: the sole association is canonical
            conn.execute(
                """UPDATE article_tickers SET is_canonical = 1
                   WHERE dedup_group_id = ? AND ticker = ?""",
                (group_id, ticker),
            )
            canonical_count += 1
            # Track the winning article
            row = conn.execute(
                "SELECT article_id FROM article_tickers WHERE dedup_group_id = ? AND ticker = ?",
                (group_id, ticker),
            ).fetchone()
            if row:
                winners_set.add((row[0], ticker))
        else:
            # Multi-article: run select_canonical
            article_rows = conn.execute("""
                SELECT DISTINCT a.article_id, a.source_type, a.title, a.published_utc,
                       a.article_url, a.description
                FROM articles a
                JOIN article_tickers at ON at.article_id = a.article_id
                WHERE at.dedup_group_id = ? AND at.ticker = ?
            """, (group_id, ticker)).fetchall()

            if len(article_rows) < 2:
                # Edge case: fewer than 2 after dedup — pick the one
                for (aid, *_) in article_rows:
                    conn.execute(
                        "UPDATE article_tickers SET is_canonical = 1 WHERE article_id = ? AND ticker = ?",
                        (aid, ticker),
                    )
                    canonical_count += 1
                    winners_set.add((aid, ticker))
                continue

            # select_canonical and AssetCandidate are in this module
            from datetime import datetime, timezone

            candidates = []
            article_ids = []
            for (aid, source_type, title, pub_utc, url, desc) in article_rows:
                article_ids.append(aid)
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
                    ticker_primary=ticker,
                    body_md=desc or "",
                ))

            try:
                winner = select_canonical(candidates)
                # Find the winning article_id by matching the candidate
                winner_aid = None
                for i, cand in enumerate(candidates):
                    if (cand.source_type == winner.source_type
                        and cand.title == winner.title
                        and cand.ticker_primary == winner.ticker_primary):
                        winner_aid = article_ids[i]
                        break
                if winner_aid is None:
                    winner_aid = article_ids[0]  # fallback

                conn.execute(
                    "UPDATE article_tickers SET is_canonical = 1 WHERE article_id = ? AND ticker = ?",
                    (winner_aid, ticker),
                )
                canonical_count += 1
                winners_set.add((winner_aid, ticker))
            except (ValueError, IndexError):
                # Fallback: first article wins
                conn.execute(
                    "UPDATE article_tickers SET is_canonical = 1 WHERE article_id = ? AND ticker = ?",
                    (article_ids[0], ticker),
                )
                canonical_count += 1
                winners_set.add((article_ids[0], ticker))

    conn.commit()

    # Recompute articles.is_canonical as derived
    # Article is canonical if ANY of its article_tickers associations is canonical
    conn.execute("UPDATE articles SET is_canonical = 0")
    winner_article_ids = set(aid for aid, _ in winners_set)
    if winner_article_ids:
        placeholders = ",".join("?" for _ in winner_article_ids)
        conn.execute(
            f"UPDATE articles SET is_canonical = 1 WHERE article_id IN ({placeholders})",
            tuple(winner_article_ids),
        )
    conn.commit()

    after_ats = conn.execute(
        "SELECT COUNT(*) FROM article_tickers WHERE dedup_group_id IS NOT NULL"
    ).fetchone()[0]
    canon_ats = conn.execute(
        "SELECT COUNT(*) FROM article_tickers WHERE is_canonical = 1"
    ).fetchone()[0]

    return {
        "before_at_count": before_ats,
        "after_at_count": after_ats,
        "canonical_associations": canon_ats,
        "winner_article_ids": len(winner_article_ids),
    }


# ── B3: Cluster field computation ────────────────────────────────────────────

_SOURCE_CLASS_PRIORITY: dict[str, int] = {
    "corporate_press_release": 0,
    "issuer_disclosure": 1,
    "official_government": 2,
    "reported_news": 3,
    "analysis_opinion": 4,
    "aggregated_unknown": 5,
    "structured_market_data": 6,
}


def compute_cluster_fields(
    members: list[dict],
) -> dict[str, str | None]:
    """Compute cluster_first_available_at and representative_document_id.

    cluster_first_available_at: the earliest available_at in the cluster.
    representative_document_id: the document with the highest-priority
    source_class. Ties are broken by earliest available_at.

    Returns dict with keys: cluster_first_available_at, representative_document_id.
    """
    if not members:
        return {
            "cluster_first_available_at": None,
            "representative_document_id": None,
        }

    # Earliest available_at
    availability = [m["available_at"] for m in members if m.get("available_at")]
    first_available = min(availability) if availability else None

    # Representative: highest-priority source_class, tie-break by earliest available_at
    def _rank(m: dict) -> tuple[int, str, str]:
        sc = m.get("source_class", "aggregated_unknown")
        pri = _SOURCE_CLASS_PRIORITY.get(sc, 99)
        at = m.get("available_at", "9999-12-31T00:00:00Z")
        return (pri, at, m.get("document_id", ""))

    representative = min(members, key=_rank)
    rep_id = representative.get("document_id")

    return {
        "cluster_first_available_at": first_available,
        "representative_document_id": rep_id,
    }
