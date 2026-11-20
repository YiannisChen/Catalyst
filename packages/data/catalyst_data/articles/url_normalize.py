"""Conservative URL normalization (M3-5; execution-lock §F).

Same-group membership requires the exact normalized URL. Unresolved ambiguity
(malformed URL, non-http(s) scheme, no host) returns ``NormalizedUrl(unknown=True)``
and is never used as a membership key.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from catalyst_data.dedup.cross_source import _TRACKING_PREFIXES


@dataclass(frozen=True)
class NormalizedUrl:
    value: str | None  # set iff unknown is False
    unknown: bool


def normalize_url(url: str | None) -> NormalizedUrl:
    """Normalize one URL with the locked conservative policy (§F).

    Scheme/host lowercased, default ports 80/443 removed, fragment stripped,
    path case preserved, query parsed with ``parse_qsl(keep_blank_values=True)``
    and sorted by ``(key, value)`` after dropping tracking prefixes exactly
    ``_TRACKING_PREFIXES = ("utm_", "ref_", "mc_")``.
    """
    if url is None:
        return NormalizedUrl(None, True)
    if not isinstance(url, str):
        return NormalizedUrl(None, True)
    raw = url.strip()
    if not raw:
        return NormalizedUrl(None, True)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return NormalizedUrl(None, True)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return NormalizedUrl(None, True)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return NormalizedUrl(None, True)
    if port is not None and port not in (80, 443):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    path = parsed.path or ""
    if path == "/":
        path = ""
    elif path.endswith("/") and path.count("/") == 2:
        path = path.rstrip("/")

    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith(_TRACKING_PREFIXES)
    ]
    query = urlencode(sorted(query_items, key=lambda item: (item[0], item[1])))

    return NormalizedUrl(
        urlunsplit((scheme, netloc, path, query, "")), False
    )


__all__ = ["NormalizedUrl", "normalize_url"]
