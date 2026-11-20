"""Map internal registry sources to the six public data-readiness categories.

A provider never determines whether a news request covers the target or peers:
callers must supply the recorded task scope. This maps identity, not completeness,
source activation, evidence eligibility, or permission to use an article.

Auxiliary sources have explicit exclusions with reasons; exclusion from this
public readiness projection is not a provider failure or a new public category.
"""
from __future__ import annotations

import json
from pathlib import Path

from catalyst_data.ingestion import sources as source_registry

CATEGORIES = ("price_bars", "sec_filings", "target_news", "peer_news", "web_search", "supplementary_news")
SCOPES = {"target": "target_news", "peer": "peer_news", "supplementary": "supplementary_news"}
MANIFEST = source_registry.REGISTRY.with_name("source_categories_v1.json")
_FIXED = {
    "daily:price_bars": "price_bars",
    "daily:sec": "sec_filings",
    "daily:discovery": "web_search",
    "daily:discovery_v2": "web_search",
    "daily:kitco": "supplementary_news",
}


def validate(mapping: dict, registry: dict) -> None:
    """Fail on registry drift or ambiguous/unknown routes, including inactive IDs."""
    if not isinstance(mapping, dict) or set(mapping) != {"schema_version", "categories", "sources"}:
        raise ValueError("invalid source-category mapping fields")
    if type(mapping["schema_version"]) is not int or mapping["schema_version"] != 1:
        raise ValueError("unsupported source-category mapping version")
    categories = mapping["categories"]
    if not isinstance(categories, list) or any(not isinstance(c, str) for c in categories):
        raise ValueError("invalid public categories")
    if len(categories) != len(CATEGORIES) or set(categories) != set(CATEGORIES):
        raise ValueError("public categories must be the six contract categories")
    if not isinstance(registry, dict) or not isinstance(registry.get("sources"), list):
        raise ValueError("invalid source registry")
    rows = registry["sources"]
    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in rows):
        raise ValueError("invalid source registry identity")
    known = {row["id"]: row for row in rows}
    if len(known) != len(rows):
        raise ValueError("duplicate source registry identity")
    routes = mapping["sources"]
    if not isinstance(routes, dict) or set(routes) != set(known):
        raise ValueError("mapping must contain exactly every registry source ID")
    for source_id, route in routes.items():
        if not isinstance(route, dict):
            raise ValueError("invalid source route")
        if source_id in _FIXED:
            if route != {"category": _FIXED[source_id]}:
                raise ValueError("fixed source category does not match its registry role")
        elif known[source_id].get("category") in ("news", "press_release"):
            if set(route) != {"by_scope"} or route["by_scope"] != SCOPES:
                raise ValueError("news route requires complete explicit target/peer/supplementary scopes")
        else:
            if set(route) != {"excluded"} or not isinstance(route["excluded"], str) or not route["excluded"].strip():
                raise ValueError("auxiliary registry source requires explicit readiness exclusion reason")


def load(path: str | Path | None = None, *, registry: dict | None = None) -> dict:
    """Load a versioned mapping beside the registry and validate its full ID set."""
    try:
        mapping = json.loads(Path(path if path is not None else MANIFEST).read_text())
    except (OSError, ValueError):
        raise ValueError("source-category manifest is missing or invalid JSON") from None
    validate(mapping, source_registry.load() if registry is None else registry)
    return mapping


def category_for(source_id: str, *, scope: str | None = None, mapping: dict | None = None) -> str:
    """Resolve one recorded task scope; never infer scope from provider or ticker."""
    if scope is not None and (not isinstance(scope, str) or scope not in SCOPES):
        raise ValueError("unknown news scope")
    if mapping is None:
        mapping = load()
    else:
        validate(mapping, source_registry.load())
    if not isinstance(source_id, str) or source_id not in mapping["sources"]:
        raise ValueError("unknown registry source ID")
    route = mapping["sources"][source_id]
    if "excluded" in route:
        raise ValueError("source has no public readiness category: " + route["excluded"])
    if "category" in route:
        return route["category"]
    if scope is None:
        raise ValueError("news source requires explicit task scope")
    return route["by_scope"][scope]
