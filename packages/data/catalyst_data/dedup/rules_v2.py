"""Pinned d1-rules-v2 relation derivation."""
from __future__ import annotations

import sqlite3

from catalyst_data.dedup import (
    links,
    origin_assign,
    publisher_registry,
    relations,
    url_identity_registry,
)
from catalyst_data.dedup.publisher_registry import PUBLISHER_REGISTRY

RULE_VERSION = "d1-rules-v2"
PINNED_URL_REGISTRY_SHA256 = "98e8fd21bce32a5f66d9cdd0c0aae41a3ae7c740b47642eb35676ef47f1bc072"
PINNED_PUBLISHER_REGISTRY_SHA256 = "f06c43bb35b22a6652866e78453a7a53c13111545a89020271cf704fc931b3a6"


def identity() -> dict[str, str]:
    return {
        "rule_version": RULE_VERSION,
        "url_identity_registry_sha256": PINNED_URL_REGISTRY_SHA256,
        "publisher_registry_sha256": PINNED_PUBLISHER_REGISTRY_SHA256,
    }


def check_pins() -> None:
    current_url = url_identity_registry.url_registry_sha256()
    current_publisher = publisher_registry.registry_sha256()
    if current_url != PINNED_URL_REGISTRY_SHA256:
        raise ValueError("URL identity registry SHA does not match d1-rules-v2 pin")
    if current_publisher != PINNED_PUBLISHER_REGISTRY_SHA256:
        raise ValueError("publisher registry SHA does not match d1-rules-v2 pin")


def gate(discovered_at: str) -> None:
    discovered_at = relations._time(discovered_at, "discovered_at")
    url_identity_registry.validate_registry()
    origin_assign._validate_registry(PUBLISHER_REGISTRY)
    entries = [*url_identity_registry.URL_IDENTITY_REGISTRY, *PUBLISHER_REGISTRY]
    for entry in entries:
        start = entry.effective_from
        end = entry.effective_until
        if discovered_at < start or (end is not None and discovered_at >= end):
            raise ValueError("discovered_at is outside a registry entry's effective interval")


def _input_fields(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return (value.get("provider"), value.get("publisher"),
                value.get("content_state"), value.get("kind"))
    if len(value) < 4:
        raise ValueError("origin input must include provider, publisher, content_state, and kind")
    return tuple(value[:4])


def _same_publisher_family(left_inputs, right_inputs) -> bool:
    if left_inputs is None or right_inputs is None:
        return False
    left = (left_inputs[0], left_inputs[1], left_inputs[2], left_inputs[3], None)
    right = (right_inputs[0], right_inputs[1], right_inputs[2], right_inputs[3], None)
    left_families = origin_assign._publisher_origins([left], PUBLISHER_REGISTRY)
    right_families = origin_assign._publisher_origins([right], PUBLISHER_REGISTRY)
    return bool(left_families & right_families)


def classify_pair_v2(left: links.EvidenceRecord, right: links.EvidenceRecord,
                     left_inputs, right_inputs) -> set[links.Link]:
    if left.record_id == right.record_id:
        return set()
    left_inputs = _input_fields(left_inputs)
    right_inputs = _input_fields(right_inputs)
    found: set[links.Link] = set()
    if (
        links._present(left.provider_record_id)
        and links._present(right.provider_record_id)
        and left.provider.strip().casefold() == right.provider.strip().casefold()
        and left.provider_record_id.strip() == right.provider_record_id.strip()
    ):
        found.add(("same_article", "provider_record_id"))

    left_url = links.normalize_identity_url(left.url)
    right_url = links.normalize_identity_url(right.url)
    if left_url is not None and left_url == right_url and left_inputs and right_inputs:
        left_publisher = left_inputs[1]
        right_publisher = right_inputs[1]
        if (isinstance(left_publisher, str) and left_publisher.strip()
                and isinstance(right_publisher, str) and right_publisher.strip()
                and url_identity_registry.url_role(left_inputs[0], left_publisher) == "article"
                and url_identity_registry.url_role(right_inputs[0], right_publisher) == "article"):
            found.add(("same_article", "canonical_url"))

    if (
        left.release_id_confirmed and right.release_id_confirmed
        and left.release_namespace.strip().casefold() == right.release_namespace.strip().casefold()
        and left.release_id.strip() == right.release_id.strip()
    ):
        found.add(("same_release", "confirmed_release_id"))

    if (
        left.body_fingerprint_confirmed and right.body_fingerprint_confirmed
        and left.fingerprint_version == right.fingerprint_version
        and left.body_fingerprint == right.body_fingerprint
    ):
        relation_type = "same_article" if _same_publisher_family(left_inputs, right_inputs) else "same_release"
        found.add((relation_type, "full_body_fingerprint"))
    return found


def rerun_relations_v2(conn: sqlite3.Connection, *, discovered_at: str) -> dict:
    discovered_at = relations._time(discovered_at, "discovered_at")
    check_pins()
    gate(discovered_at)
    report = relations.rerun_classified_relations(
        conn,
        discovered_at=discovered_at,
        rule_version=RULE_VERSION,
        classify_pair=classify_pair_v2,
        basis_value=relations._legacy_basis_value,
        prepare=origin_assign.ensure_schema,
    )
    return {
        **report,
        "url_identity_registry_sha256": PINNED_URL_REGISTRY_SHA256,
        "publisher_registry_sha256": PINNED_PUBLISHER_REGISTRY_SHA256,
    }
