from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
