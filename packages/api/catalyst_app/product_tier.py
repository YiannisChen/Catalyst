"""Product tier of an answer: what the user is told, derived from the stored snapshot (add-only, 2026-11-16).

The research outcome (ANSWERED / ABSTAINED / TIER_ONLY) is unchanged. This module only describes it for the
product, using the price facts every run already computes:

* CONFIRMED                the strict gate published a cause;
* REPORTED                 an abstained answer has a verbatim reported reason;
* MOVED_WITH_GROUP         no cause, and the stock moved with its peers (or less than them);
* LARGER_THAN_GROUP        no cause; same direction as peers but by clearly more, so part of the move is unexplained;
* NOTHING_FOUND            no cause; the stock moved against its peers, they did not move, or the references disagree;
* ORDINARY_MOVE            the move was too small to need a cause (TIER_ONLY).

Group rule (checked on 97 local days, see the answer-tiers design): group = peer median (sector when there are no
peers); residual = stock - group. "With the group" needs the same sign, |group| >= 0.75 points, |residual| <= 1.5
points and |residual| <= half the stock's move. Leads published after the close are dropped.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping

MIN_GROUP_PCT = 0.75
MAX_RESIDUAL_PCT = 1.5
FLAT_GROUP_PCT = 0.3


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
    if "T" not in text and " " in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def group_context(facts: Mapping[str, Any] | None) -> dict[str, Any]:
    facts = facts or {}
    stock = _num(facts.get("target_return_pct"))
    peers_block = facts.get("peers") if isinstance(facts.get("peers"), Mapping) else {}
    sector_block = facts.get("sector") if isinstance(facts.get("sector"), Mapping) else {}
    peers = _num(peers_block.get("median_return_pct"))
    sector = _num(sector_block.get("return_pct"))
    context: dict[str, Any] = {
        "relation": "unknown",
        "stock_pct": stock,
        "peers_median_pct": peers,
        "peers_count": peers_block.get("count") if isinstance(peers_block.get("count"), int) else None,
        "sector_pct": sector,
        "sector_ticker": sector_block.get("ticker") if isinstance(sector_block.get("ticker"), str) else None,
        "residual_pct": None,
    }
    group = peers if peers is not None else sector
    if stock is None or group is None:
        return context
    residual = stock - group
    context["residual_pct"] = round(residual, 4)
    if peers is not None and sector is not None and abs(sector) >= FLAT_GROUP_PCT and (peers > 0) != (sector > 0):
        context["relation"] = "disagree"
    elif abs(group) < FLAT_GROUP_PCT:
        context["relation"] = "flat"
    elif (stock > 0) != (group > 0):
        context["relation"] = "against"
    elif abs(group) >= MIN_GROUP_PCT and abs(residual) <= MAX_RESIDUAL_PCT and abs(residual) <= abs(stock) / 2:
        context["relation"] = "in_line"
    elif abs(stock) < abs(group):
        context["relation"] = "smaller"
    else:
        context["relation"] = "larger"
    return context


def product_tier(outcome: Any, context: Mapping[str, Any]) -> str:
    if outcome in ("ANSWERED", "WEB_SOURCE_SUPPORTED"):
        return "CONFIRMED"
    if outcome == "TIER_ONLY":
        return "ORDINARY_MOVE"
    snapshot = context.get("snapshot")
    reported = context.get("reported_reason")
    if outcome == "ABSTAINED" and (
        isinstance(reported, dict)
        or isinstance(snapshot, Mapping) and isinstance(snapshot.get("reported_reason"), dict)
    ):
        return "REPORTED"
    relation = context.get("relation")
    if relation in ("in_line", "smaller"):
        return "MOVED_WITH_GROUP"
    if relation == "larger":
        return "LARGER_THAN_GROUP"
    return "NOTHING_FOUND"


def with_product_fields(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The snapshot plus ``product_tier`` and ``group_context``; abstention leads after the cutoff are dropped."""
    answer = deepcopy(dict(snapshot))
    context = group_context(answer.get("facts") if isinstance(answer.get("facts"), Mapping) else None)
    answer["group_context"] = context
    answer["product_tier"] = product_tier(answer.get("outcome"), {**context, "snapshot": answer})
    window = answer.get("window") if isinstance(answer.get("window"), Mapping) else {}
    cutoff = _when(window.get("cutoff_at"))
    abstention = answer.get("abstention")
    if cutoff is not None and isinstance(abstention, dict) and isinstance(abstention.get("leads"), list):
        abstention["leads"] = [
            lead for lead in abstention["leads"]
            if not isinstance(lead, Mapping) or (_when(lead.get("published_at")) or cutoff) <= cutoff
        ]
    return answer


__all__ = ["group_context", "product_tier", "with_product_fields"]
