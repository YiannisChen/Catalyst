"""Append-only deterministic cross-provider duplicate links and origin assertions.

This module records relationships between raw record identities. It never
deletes or chooses a canonical record, and title text is intentionally not an
identity signal.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from catalyst_data.storage.atomic import atomic_savepoint

RelationType = Literal["same_article", "same_release"]
Link = tuple[RelationType, str]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRACKING_PREFIXES = ("utm_", "ref_", "mc_")


@dataclass(frozen=True)
class EvidenceRecord:
    """Minimal stable identity fields for one preserved raw record."""

    record_id: str
    provider: str
    provider_record_id: str | None = None
    url: str | None = None
    release_id: str | None = None
    release_namespace: str | None = None
    release_id_confirmed: bool = False
    body_fingerprint: str | None = None
    fingerprint_version: str | None = None
    body_fingerprint_confirmed: bool = False
    title: str | None = None  # Retained as context; never establishes a link.

    def __post_init__(self) -> None:
        if not self.record_id.strip():
            raise ValueError("record_id must be non-empty")
        if not self.provider.strip():
            raise ValueError("provider must be non-empty")
        if self.release_id_confirmed and not (
            _present(self.release_id) and _present(self.release_namespace)
        ):
            raise ValueError("confirmed release IDs require an ID and namespace")
        if self.body_fingerprint_confirmed:
            if not _present(self.body_fingerprint) or not _present(self.fingerprint_version):
                raise ValueError("confirmed body fingerprints require a hash and version")
            if not _SHA256.fullmatch(self.body_fingerprint or ""):
                raise ValueError("body_fingerprint must be a lowercase SHA-256 hex digest")


@dataclass(frozen=True)
class DuplicateLink:
    record_a: str
    record_b: str
    relation_type: RelationType
    evidence_kind: str


def _present(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def normalize_identity_url(url: str | None) -> str | None:
    """Return the contract's conservative URL identity, or ``None`` if invalid.

    Host and scheme are case-normalized, fragments and known tracking/AMP query
    parameters are removed, and common ``/amp`` path suffixes are mapped to
    their source path. Other path/query distinctions are preserved.
    """
    if not _present(url):
        return None
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    if scheme not in ("http", "https") or not host or parsed.username or parsed.password:
        return None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    is_default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if port is None or is_default_port else f"{host}:{port}"

    path = parsed.path or ""
    if path.lower().endswith("/amp"):
        path = path[:-4]
    if path == "/":
        path = ""
    elif path.endswith("/") and path.count("/") == 2:
        path = path.rstrip("/")

    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith(_TRACKING_PREFIXES) and key.lower() != "amp"
    ]
    # Normalize independent keys while preserving the relative order of repeated
    # keys, whose order can carry meaning to the receiving application.
    query = urlencode(sorted(query_items, key=lambda item: item[0]))
    return urlunsplit((scheme, netloc, path, query, ""))


def classify_pair(left: EvidenceRecord, right: EvidenceRecord) -> set[Link]:
    """Return every deterministically supported relation for a record pair.

    Provider IDs are scoped by provider. Canonical URLs establish article
    identity across providers. Release IDs and explicitly confirmed,
    versioned full-body SHA-256 fingerprints establish release identity.
    Similar titles, unconfirmed IDs, and unconfirmed hashes return no link.
    """
    if left.record_id == right.record_id:
        return set()

    links: set[Link] = set()
    if (
        _present(left.provider_record_id)
        and _present(right.provider_record_id)
        and left.provider.strip().casefold() == right.provider.strip().casefold()
        and left.provider_record_id.strip() == right.provider_record_id.strip()
    ):
        links.add(("same_article", "provider_record_id"))

    left_url = normalize_identity_url(left.url)
    right_url = normalize_identity_url(right.url)
    if left_url is not None and left_url == right_url:
        links.add(("same_article", "canonical_url"))

    if (
        left.release_id_confirmed
        and right.release_id_confirmed
        and left.release_namespace.strip().casefold() == right.release_namespace.strip().casefold()  # type: ignore[union-attr]
        and left.release_id.strip() == right.release_id.strip()  # type: ignore[union-attr]
    ):
        links.add(("same_release", "confirmed_release_id"))

    if (
        left.body_fingerprint_confirmed
        and right.body_fingerprint_confirmed
        and left.fingerprint_version == right.fingerprint_version
        and left.body_fingerprint == right.body_fingerprint
    ):
        links.add(("same_release", "full_body_fingerprint"))
    return links




def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create append-only relationship tables; safe to call repeatedly."""
    statements = (
        """
        CREATE TABLE IF NOT EXISTS duplicate_links (
            record_a TEXT NOT NULL,
            record_b TEXT NOT NULL,
            relation_type TEXT NOT NULL CHECK (relation_type IN ('same_article', 'same_release')),
            evidence_kind TEXT NOT NULL,
            PRIMARY KEY (record_a, record_b, relation_type, evidence_kind),
            CHECK (record_a < record_b)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS origin_assertions (
            assertion_id TEXT PRIMARY KEY,
            record_id TEXT NOT NULL,
            origin_id TEXT,
            method TEXT NOT NULL,
            evidence_ref TEXT
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_origin_assertions_record
            ON origin_assertions (record_id)
        """,
        """
        CREATE TRIGGER IF NOT EXISTS duplicate_links_no_update
        BEFORE UPDATE ON duplicate_links
        BEGIN
            SELECT RAISE(ABORT, 'duplicate_links is append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS duplicate_links_no_delete
        BEFORE DELETE ON duplicate_links
        BEGIN
            SELECT RAISE(ABORT, 'duplicate_links is append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS origin_assertions_no_update
        BEFORE UPDATE ON origin_assertions
        BEGIN
            SELECT RAISE(ABORT, 'origin_assertions is append-only');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS origin_assertions_no_delete
        BEFORE DELETE ON origin_assertions
        BEGIN
            SELECT RAISE(ABORT, 'origin_assertions is append-only');
        END
        """,
    )
    with atomic_savepoint(conn, "duplicate_link_schema"):
        for statement in statements:
            conn.execute(statement)


def persist_pair_links(
    conn: sqlite3.Connection, left: EvidenceRecord, right: EvidenceRecord
) -> int:
    """Append deterministic links for a pair; return the number newly added."""
    ensure_schema(conn)
    pair = tuple(sorted((left.record_id, right.record_id)))
    rows = [
        (pair[0], pair[1], relation, evidence_kind)
        for relation, evidence_kind in sorted(classify_pair(left, right))
    ]
    before = conn.total_changes
    conn.executemany(
        """INSERT OR IGNORE INTO duplicate_links
           (record_a, record_b, relation_type, evidence_kind)
           VALUES (?, ?, ?, ?)""",
        rows,
    )
    return conn.total_changes - before


def list_duplicate_links(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    """Read links in stable order without modifying source records."""
    ensure_schema(conn)
    return [
        (row[0], row[1], row[2], row[3])
        for row in conn.execute(
            """SELECT record_a, record_b, relation_type, evidence_kind
               FROM duplicate_links
               ORDER BY record_a, record_b, relation_type, evidence_kind"""
        )
    ]


def append_origin_assertion(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    origin_id: str | None,
    method: str,
    evidence_ref: str | None = None,
) -> bool:
    """Append an explicit origin assertion; return false for exact replays.

    ``origin_id=None`` records an unresolved assertion. No title-based or
    provider-based origin is synthesized here.
    """
    if not record_id.strip():
        raise ValueError("record_id must be non-empty")
    if not method.strip():
        raise ValueError("method must be non-empty")
    if origin_id is not None and not origin_id.strip():
        raise ValueError("origin_id must be non-empty or None")
    payload = {
        "record_id": record_id,
        "origin_id": origin_id,
        "method": method,
        "evidence_ref": evidence_ref,
    }
    assertion_id = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    ensure_schema(conn)
    before = conn.total_changes
    conn.execute(
        """INSERT OR IGNORE INTO origin_assertions
           (assertion_id, record_id, origin_id, method, evidence_ref)
           VALUES (?, ?, ?, ?, ?)""",
        (assertion_id, record_id, origin_id, method, evidence_ref),
    )
    return conn.total_changes > before


def origin_for(conn: sqlite3.Connection, record_id: str) -> str | None:
    """Resolve one unambiguous explicit origin; missing/conflicting stays None."""
    ensure_schema(conn)
    origins = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT origin_id FROM origin_assertions WHERE record_id = ? AND origin_id IS NOT NULL",
            (record_id,),
        )
    }
    if len(origins) != 1:
        return None
    return next(iter(origins))


__all__ = [
    "DuplicateLink",
    "EvidenceRecord",
    "RelationType",
    "append_origin_assertion",
    "classify_pair",
    "ensure_schema",
    "list_duplicate_links",
    "normalize_identity_url",
    "origin_for",
    "persist_pair_links",
]
