"""Post-retrieval tier-aware re-ranking (pure functions, no DB, no I/O).

Composable policies:
  - boost_by_tier: multiply each result's score by a tier multiplier,
    then re-sort descending. Preserves original score as _raw_rrf_score.
  - cap_t5_opinion: ensures T5 (opinion) articles ≤ max_pct of final top-k.
    Caps, does not exclude — trims lowest-scored T5 entries.
  - apply_retrieval_policy: compose boost → cap → slice top_k.
"""

from __future__ import annotations

from typing import Any

# Default tier multipliers
DEFAULT_TIER_BOOST: dict[int, float] = {
    1: 1.5,
    2: 1.3,
    3: 1.1,
    4: 1.0,
    5: 0.9,
    6: 1.0,
}


def boost_by_tier(
    results: list[dict[str, Any]],
    *,
    tiers: dict[int, float] | None = None,
) -> list[dict[str, Any]]:
    """Multiply each result's score by its source_tier multiplier, re-sort.

    The original score is preserved as `_raw_rrf_score` on each result for
    audit/debugging.  If a result has no `source_tier`, T4 (1.0) is assumed.

    Returns a *new* list; input is not mutated.
    """
    multipliers = tiers or DEFAULT_TIER_BOOST
    boosted = []
    for r in results:
        tier = r.get("source_tier", 4)
        mult = multipliers.get(tier, 1.0)
        raw = r.get("rrf_score", r.get("score", 0.0))
        new_r = dict(r)
        new_r["_raw_rrf_score"] = raw
        new_r["rrf_score"] = raw * mult
        boosted.append(new_r)
    boosted.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
    return boosted


def cap_t5_opinion(
    results: list[dict[str, Any]],
    *,
    max_pct: float = 0.30,
) -> list[dict[str, Any]]:
    """Ensure T5 (opinion) articles ≤ max_pct of the result list.

    Trims lowest-scored T5 entries from the bottom until the cap is satisfied.
    If there are fewer than 3 total results, the cap is skipped (no point
    capping with tiny k).  Returns a new list.
    """
    if len(results) < 3:
        return list(results)

    t5_entries = [r for r in results if r.get("source_tier") == 5]
    if not t5_entries:
        return list(results)

    max_t5 = max(1, int(len(results) * max_pct))
    if len(t5_entries) <= max_t5:
        return list(results)

    # Trim lowest-scored T5 entries (they're at the bottom after boost+sort)
    t5_to_keep = t5_entries[:max_t5]
    t5_ids_to_keep = {id(r) for r in t5_to_keep}

    return [r for r in results
            if r.get("source_tier") != 5 or id(r) in t5_ids_to_keep]


def apply_retrieval_policy(
    results: list[dict[str, Any]],
    top_k: int = 12,
    *,
    tier_multipliers: dict[int, float] | None = None,
    t5_max_pct: float = 0.30,
) -> list[dict[str, Any]]:
    """Compose: tier_boost → cap_t5_opinion → slice top_k.

    Pure function — no side effects, no DB access.
    """
    boosted = boost_by_tier(results, tiers=tier_multipliers)
    capped = cap_t5_opinion(boosted, max_pct=t5_max_pct)
    return capped[:top_k]
