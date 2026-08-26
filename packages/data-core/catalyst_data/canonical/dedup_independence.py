"""V1.1 canonical dedup/independence computation and persistence (M3-6).

Execution-lock §A.7/A.8/§G: union-find over exact normalized URL / content
hash / provider document ID / known syndication relation. Ambiguous lineage is
never independent by default. ``persist_dedup_independence`` atomically writes
the IDs on the M3-5B asset set before M3-7 audit.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from catalyst_data.articles.url_normalize import normalize_url
from catalyst_data.canonical.ids import sha256_identity

DEDUP_ALGORITHM_VERSION = "catalyst_dedup_v1.0"
INDEPENDENCE_ALGORITHM_VERSION = "catalyst_independence_v1.0"

KNOWN_SYNDICATION_PATH = Path("data/manifests/known_syndication_v1.json")


@dataclass(frozen=True)
class DedupResult:
    cluster_id_by_asset: Mapping[str, str]  # asset_id -> v1:dedup:...
    algorithm_version: str  # "catalyst_dedup_v1.0"


@dataclass(frozen=True)
class IndependenceResult:
    group_id_by_asset: Mapping[str, str | None]  # None = unknown lineage
    unknown_independence_asset_count: int
    eligible_reported_news_group_count: int
    algorithm_version: str  # "catalyst_independence_v1.0"


def load_known_syndication() -> list[dict[str, object]]:
    """Load committed syndication relations (empty default when absent)."""
    if not KNOWN_SYNDICATION_PATH.is_file():
        return []
    data = json.loads(KNOWN_SYNDICATION_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != "known_syndication_v1":
        raise ValueError(
            f"known_syndication manifest has unexpected schema_version: "
            f"{data.get('schema_version')!r}"
        )
    relations = data.get("relations") or []
    if not isinstance(relations, list):
        raise ValueError("known_syndication manifest relations must be a list")
    return relations


def _union_find(asset_ids: list[str]) -> dict[str, str]:
    parent = {aid: aid for aid in asset_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    return parent


def _components(
    canonical_assets: Sequence[Mapping[str, object]],
    known_syndication: Sequence[Mapping[str, object]] | None,
) -> tuple[list[list[str]], set[str]]:
    asset_ids = [str(a["asset_id"]) for a in canonical_assets]
    parent = _union_find(asset_ids)

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    url_index: dict[str, list[str]] = {}
    hash_index: dict[str, list[str]] = {}
    doc_index: dict[str, list[str]] = {}
    for asset in canonical_assets:
        aid = str(asset["asset_id"])
        normalized = normalize_url(asset.get("canonical_url"))
        if normalized is not None and not normalized.unknown and normalized.value:
            url_index.setdefault(normalized.value, []).append(aid)
        content_hash = asset.get("content_hash")
        if content_hash:
            hash_index.setdefault(str(content_hash), []).append(aid)
        document_id = asset.get("document_id")
        if document_id:
            doc_index.setdefault(str(document_id), []).append(aid)

    for ids in url_index.values():
        for other in ids[1:]:
            union(ids[0], other)
    for ids in hash_index.values():
        for other in ids[1:]:
            union(ids[0], other)
    for ids in doc_index.values():
        for other in ids[1:]:
            union(ids[0], other)

    relations = (
        list(known_syndication)
        if known_syndication is not None
        else load_known_syndication()
    )
    syndication_members: set[str] = set()
    for rel in relations:
        if rel.get("relation") != "syndicated":
            continue
        a = rel.get("asset_id_a")
        b = rel.get("asset_id_b")
        if a in parent and b in parent:
            union(str(a), str(b))
            syndication_members.add(str(a))
            syndication_members.add(str(b))

    buckets: dict[str, list[str]] = {}
    for aid in asset_ids:
        buckets.setdefault(find(aid), []).append(aid)
    components = [sorted(members) for members in buckets.values()]
    return components, syndication_members


def compute_dedup_clusters(
    canonical_assets: Sequence[Mapping[str, object]],
    *,
    known_syndication: Sequence[Mapping[str, object]] | None = None,
) -> DedupResult:
    """Compute deterministic dedup clusters over the canonical asset set.

    Evidence precedence (§G): exact normalized URL, exact content hash, exact
    provider document ID, known syndication relation. Singleton assets still
    receive a cluster ID so dedup identity coverage is measurable.
    """
    components, _ = _components(canonical_assets, known_syndication)
    cluster_id_by_asset: dict[str, str] = {}
    for members in components:
        cluster_id = "v1:dedup:" + sha256_identity(
            {
                "algorithm": DEDUP_ALGORITHM_VERSION,
                "members": members,
            }
        )
        for aid in members:
            cluster_id_by_asset[aid] = cluster_id
    return DedupResult(
        cluster_id_by_asset=cluster_id_by_asset,
        algorithm_version=DEDUP_ALGORITHM_VERSION,
    )


def compute_independence_groups(
    assets: Sequence[Mapping[str, object]],
    source_class: str = "reported_news",
    *,
    known_syndication: Sequence[Mapping[str, object]] | None = None,
) -> IndependenceResult:
    """Compute reported-news independence groups over known-lineage assets.

    Unknown-lineage assets (singleton components with no syndication relation)
    receive ``None`` and increment ``unknown_independence_asset_count``; they
    are never independent by default.
    """
    components, syndication_members = _components(assets, known_syndication)
    by_id = {str(a["asset_id"]): a for a in assets}
    group_id_by_asset: dict[str, str | None] = {}
    unknown_count = 0
    group_ids: set[str] = set()

    for component in components:
        eligible = [
            aid
            for aid in component
            if str(by_id[aid].get("source_class") or "") == source_class
        ]
        known = [
            aid
            for aid in eligible
            if len(component) >= 2 or aid in syndication_members
        ]
        if not known:
            for aid in eligible:
                group_id_by_asset[aid] = None
                unknown_count += 1
            continue
        group_id = "v1:independence:" + sha256_identity(
            {
                "algorithm": INDEPENDENCE_ALGORITHM_VERSION,
                "members": sorted(known),
            }
        )
        for aid in eligible:
            group_id_by_asset[aid] = group_id if aid in known else None
            if aid not in known:
                unknown_count += 1
        group_ids.add(group_id)

    # Non-eligible assets are present in the mapping as None (never grouped).
    for aid in by_id:
        group_id_by_asset.setdefault(aid, None)

    return IndependenceResult(
        group_id_by_asset=group_id_by_asset,
        unknown_independence_asset_count=unknown_count,
        eligible_reported_news_group_count=len(group_ids),
        algorithm_version=INDEPENDENCE_ALGORITHM_VERSION,
    )


def load_canonical_assets(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Load the M3-5B canonical asset records for dedup/independence.

    Includes the latest content version's state-bound hash and the subtype
    primary key as the provider document identity.
    """
    rows = conn.execute(
        """
        SELECT a.asset_id, a.asset_type, a.source_class, a.canonical_url,
               a.dedup_cluster_id, a.independence_group_id,
               v.content_hash, v.version_ordinal,
               s.subtype_table, s.subtype_pk, s.subtype_pk_value
        FROM canonical_assets a
        LEFT JOIN canonical_content_versions v ON v.asset_id = a.asset_id
        LEFT JOIN canonical_subtype_assoc s ON s.asset_id = a.asset_id
            AND s.subtype_table IN ('articles', 'filings')
        """
    ).fetchall()
    latest: dict[str, dict[str, object]] = {}
    for row in rows:
        aid = row["asset_id"]
        entry = latest.setdefault(
            aid,
            {
                "asset_id": aid,
                "asset_type": row["asset_type"],
                "source_class": row["source_class"],
                "canonical_url": row["canonical_url"],
                "content_hash": None,
                "document_id": None,
                "dedup_cluster_id": row["dedup_cluster_id"],
                "independence_group_id": row["independence_group_id"],
            },
        )
        # Latest content version wins (max version_ordinal).
        if row["version_ordinal"] is not None and (
            entry.get("_ordinal") is None
            or int(row["version_ordinal"]) > int(entry["_ordinal"])
        ):
            entry["content_hash"] = row["content_hash"]
            entry["_ordinal"] = row["version_ordinal"]
        if row["subtype_pk_value"] is not None:
            entry["document_id"] = row["subtype_pk_value"]
    assets = [entry for entry in latest.values()]
    for entry in assets:
        entry.pop("_ordinal", None)
    return assets


def persist_dedup_independence(
    conn: sqlite3.Connection,
    dedup_result: DedupResult,
    independence_result: IndependenceResult,
) -> None:
    """Atomically persist dedup/independence IDs on the M3-5B asset set.

    Fails closed if a result names a missing asset, omits any canonical asset
    from the dedup result, assigns an independence group to a non-reported-news
    or unknown-lineage asset, or attempts to change a non-null unequal
    persisted ID. Equal reruns are idempotent.
    """
    known_ids = {
        row[0]
        for row in conn.execute("SELECT asset_id FROM canonical_assets").fetchall()
    }

    missing = set(dedup_result.cluster_id_by_asset) - known_ids
    if missing:
        raise ValueError(f"dedup result names unknown assets: {sorted(missing)}")
    omitted = known_ids - set(dedup_result.cluster_id_by_asset)
    if omitted:
        raise ValueError(f"dedup result omits canonical assets: {sorted(omitted)}")
    extra = set(independence_result.group_id_by_asset) - known_ids
    if extra:
        raise ValueError(f"independence result names unknown assets: {sorted(extra)}")

    source_class_by_id = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT asset_id, source_class FROM canonical_assets"
        ).fetchall()
    }
    for aid, group_id in independence_result.group_id_by_asset.items():
        if group_id is None:
            continue
        if source_class_by_id[aid] != "reported_news":
            raise ValueError(
                f"independence group assigned to non-reported_news asset {aid}"
            )

    conn.execute("BEGIN IMMEDIATE")
    try:
        for aid, cluster_id in dedup_result.cluster_id_by_asset.items():
            persisted = conn.execute(
                "SELECT dedup_cluster_id FROM canonical_assets WHERE asset_id=?",
                (aid,),
            ).fetchone()[0]
            if persisted is not None and persisted != cluster_id:
                raise ValueError(
                    f"cannot overwrite dedup_cluster_id for {aid}"
                )
            conn.execute(
                "UPDATE canonical_assets SET dedup_cluster_id=? WHERE asset_id=?",
                (cluster_id, aid),
            )
        for aid, group_id in independence_result.group_id_by_asset.items():
            persisted = conn.execute(
                "SELECT independence_group_id FROM canonical_assets WHERE asset_id=?",
                (aid,),
            ).fetchone()[0]
            if persisted is not None and persisted != group_id:
                raise ValueError(
                    f"cannot overwrite independence_group_id for {aid}"
                )
            conn.execute(
                "UPDATE canonical_assets SET independence_group_id=? WHERE asset_id=?",
                (group_id, aid),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


__all__ = [
    "DEDUP_ALGORITHM_VERSION",
    "INDEPENDENCE_ALGORITHM_VERSION",
    "DedupResult",
    "IndependenceResult",
    "compute_dedup_clusters",
    "compute_independence_groups",
    "load_canonical_assets",
    "load_known_syndication",
    "persist_dedup_independence",
]
