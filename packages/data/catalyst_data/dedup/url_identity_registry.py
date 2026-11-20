"""Versioned provider/publisher URL identity roles."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class UrlIdentityEntry:
    provider: str
    publisher: str
    url_role: str
    effective_from: str
    effective_until: str | None = None
    evidence_ref: str = ""


URL_IDENTITY_REGISTRY = (
    UrlIdentityEntry(
        "alpaca", "fiscal.ai", "attribution", "2026-10-21T00:00:00Z",
        evidence_ref=(
            "production Alpaca outbox: 300 shared fiscal.ai URLs across 868 records; "
            "2026-11-02 review: 8 independent pairs"
        ),
    ),
)


def _effective_time(value: str, field: str) -> str:
    if not isinstance(value, str) or len(value) != 20 or not value.endswith("Z"):
        raise ValueError(f"{field} must be a UTC timestamp in YYYY-MM-DDTHH:MM:SSZ form")
    try:
        from datetime import datetime
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(f"{field} must be a UTC timestamp in YYYY-MM-DDTHH:MM:SSZ form") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError(f"{field} must be a UTC timestamp in YYYY-MM-DDTHH:MM:SSZ form")
    return value


def validate_registry(entries: tuple[UrlIdentityEntry, ...] | None = None) -> None:
    if entries is None:
        entries = URL_IDENTITY_REGISTRY
    intervals: dict[tuple[str, str], list[tuple[str, str | None]]] = {}
    for entry in entries:
        if not isinstance(entry, UrlIdentityEntry):
            raise ValueError("URL identity registry entries must be UrlIdentityEntry values")
        if not entry.provider.strip() or not entry.publisher.strip():
            raise ValueError("URL identity registry keys must be nonempty")
        if entry.url_role not in {"article", "attribution"}:
            raise ValueError("unsupported URL identity role")
        start = _effective_time(entry.effective_from, "effective_from")
        end = None if entry.effective_until is None else _effective_time(
            entry.effective_until, "effective_until"
        )
        if end is not None and end <= start:
            raise ValueError("effective_until must be later than effective_from")
        key = (entry.provider.strip().casefold(), entry.publisher.strip().casefold())
        for prior_start, prior_end in intervals.setdefault(key, []):
            if (prior_end is None or start < prior_end) and (end is None or prior_start < end):
                raise ValueError("URL identity registry has overlapping entries for one key")
        intervals[key].append((start, end))


def url_registry_sha256() -> str:
    """Hash the stored registry values, including both effective bounds."""
    validate_registry()
    rows = [
        {
            "provider": entry.provider,
            "publisher": entry.publisher,
            "url_role": entry.url_role,
            "effective_from": entry.effective_from,
            "effective_until": entry.effective_until,
            "evidence_ref": entry.evidence_ref,
        }
        for entry in sorted(URL_IDENTITY_REGISTRY, key=lambda row: (
            row.provider, row.publisher, row.url_role, row.effective_from,
            row.effective_until or "", row.evidence_ref,
        ))
    ]
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"),
                         sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def url_role(provider: str, publisher: str) -> str:
    """Look up the role using this registry's strip-and-casefold key."""
    validate_registry()
    key = (provider.strip().casefold(), publisher.strip().casefold())
    matches = [entry for entry in URL_IDENTITY_REGISTRY
               if (entry.provider.strip().casefold(), entry.publisher.strip().casefold()) == key]
    if len(matches) > 1:
        raise ValueError("URL identity registry has ambiguous entries for one key")
    return matches[0].url_role if matches else "article"
