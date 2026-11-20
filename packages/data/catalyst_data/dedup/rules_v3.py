"""D1 v3: attribution records inherit origins only through same-release sources."""
from __future__ import annotations

import sqlite3

from catalyst_data.dedup import (
    origin_assign,
    publisher_registry,
    relations,
    rules_v2,
    url_identity_registry,
)

RULE_VERSION = "d1-rules-v3"
PINNED_URL_REGISTRY_SHA256 = rules_v2.PINNED_URL_REGISTRY_SHA256
PINNED_PUBLISHER_REGISTRY_SHA256 = rules_v2.PINNED_PUBLISHER_REGISTRY_SHA256


def identity() -> dict[str, str]:
    return {
        "rule_version": RULE_VERSION,
        "url_identity_registry_sha256": PINNED_URL_REGISTRY_SHA256,
        "publisher_registry_sha256": PINNED_PUBLISHER_REGISTRY_SHA256,
    }


def check_pins() -> None:
    if url_identity_registry.url_registry_sha256() != PINNED_URL_REGISTRY_SHA256:
        raise ValueError("URL identity registry SHA does not match d1-rules-v3 pin")
    if publisher_registry.registry_sha256() != PINNED_PUBLISHER_REGISTRY_SHA256:
        raise ValueError("publisher registry SHA does not match d1-rules-v3 pin")


def rerun_relations_v3(conn: sqlite3.Connection, *, discovered_at: str) -> dict:
    check_pins()
    discovered_at = relations._time(discovered_at, "discovered_at")
    rules_v2.check_pins()
    rules_v2.gate(discovered_at)
    report = relations.rerun_classified_relations(
        conn,
        discovered_at=discovered_at,
        rule_version=RULE_VERSION,
        classify_pair=rules_v2.classify_pair_v2,
        basis_value=relations._legacy_basis_value,
        prepare=origin_assign.ensure_schema,
    )
    return {
        **report,
        "url_identity_registry_sha256": PINNED_URL_REGISTRY_SHA256,
        "publisher_registry_sha256": PINNED_PUBLISHER_REGISTRY_SHA256,
    }


def assign_origin(conn: sqlite3.Connection, record_key: str, *, at: str, mode: str,
                  snapshot_relation_ids=None, registry_as_of: str | None = None) -> dict:
    return origin_assign.assign_origin(
        conn, record_key, at=at, mode=mode,
        snapshot_relation_ids=snapshot_relation_ids, registry_as_of=registry_as_of,
        rule_version=RULE_VERSION,
    )
