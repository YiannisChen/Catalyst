from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from typing import Any


VALID_RELATIONSHIP_TYPES = {"peer", "supplier", "customer", "competitor"}


class RelationshipManifestError(ValueError):
    pass


def relationship_manifest_hash(manifest: dict[str, Any]) -> str:
    edges = sorted(manifest.get("edges", []), key=lambda edge: edge["edge_id"])
    payload = {"schema_version": manifest["schema_version"], "edges": edges}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def validate_relationship_manifest(manifest: dict[str, Any], *, now: str = "2026-07-22T00:00:00Z") -> dict[str, Any]:
    seen: set[str] = set()
    now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
    for edge in manifest.get("edges", []):
        edge_id = edge.get("edge_id")
        if edge_id in seen:
            raise RelationshipManifestError("duplicate edge_id")
        seen.add(edge_id)
        if edge.get("relationship_type") not in VALID_RELATIONSHIP_TYPES:
            raise RelationshipManifestError("unknown relationship_type")
        start = date.fromisoformat(edge["effective_from"])
        end = date.fromisoformat(edge["effective_to"]) if edge.get("effective_to") else None
        if end is not None and end < start:
            raise RelationshipManifestError("invalid effective interval")
        reviewed_at = datetime.fromisoformat(edge["reviewed_at"].replace("Z", "+00:00"))
        if reviewed_at > now_dt:
            raise RelationshipManifestError("future reviewed_at")
    if manifest.get("manifest_id") != relationship_manifest_hash(manifest):
        raise RelationshipManifestError("manifest hash mismatch")
    return manifest


def effective_edges(manifest: dict[str, Any], *, ticker: str, session_date: str) -> list[dict[str, Any]]:
    validate_relationship_manifest(manifest)
    session = date.fromisoformat(session_date)
    out = []
    for edge in manifest.get("edges", []):
        start = date.fromisoformat(edge["effective_from"])
        end = date.fromisoformat(edge["effective_to"]) if edge.get("effective_to") else None
        if edge["from_ticker"] == ticker and start <= session and (end is None or session <= end):
            out.append(edge)
    return sorted(out, key=lambda edge: edge["edge_id"])
