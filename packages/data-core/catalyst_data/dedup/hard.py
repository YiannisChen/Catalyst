from __future__ import annotations

import hashlib
import re
from datetime import datetime


def compute_dedup_fingerprint(title: str, published_utc: str) -> str:
    """Compute SHA256 fingerprint from normalized title + 2-hour time window."""
    title_norm = re.sub(r"[^\w\s]", "", title).lower().strip()
    dt = datetime.fromisoformat(published_utc.replace("Z", "+00:00"))
    window = dt.replace(
        hour=(dt.hour // 2) * 2, minute=0, second=0, microsecond=0
    )
    return hashlib.sha256(f"{title_norm}|{window.isoformat()}".encode()).hexdigest()[
        :16
    ]


def deduplicate_articles(articles: list[dict]) -> list[dict]:
    """Remove duplicate articles by title+time fingerprint. Preserves first occurrence."""
    seen: set[str] = set()
    result: list[dict] = []
    for article in articles:
        title = article.get("title", "")
        pub = article.get("published_utc", "")
        if not title or not pub:
            result.append(article)
            continue
        fp = compute_dedup_fingerprint(title, pub)
        if fp not in seen:
            seen.add(fp)
            result.append(article)
    return result
