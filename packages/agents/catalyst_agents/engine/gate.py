"""The desk gate with the v2 recency veto.

``select_and_gate`` is the desk gate (catalyst_agents.attribution.extraction.select_and_gate) with the LEAD-
attribution/same-evidence EVENT pairing rule and two hooks, both off by default:
- ``stale(cluster)``: the cluster is not eligible and, unless ``stale_counts``, not a session company event;
- ``extra_ok(cluster)``: an extra eligibility condition.
``gate`` is what the v2 graph calls: with the veto, a stale top cluster ends the stage (no fall-through to the next
cluster, which in replay published other wrong answers).
"""
from __future__ import annotations

from typing import Callable

from catalyst_agents.attribution.extraction import (  # noqa: F401
    _GROUP_EVENTS,
    _GROUP_EVENTS_FOR,
    _MATERIAL_ISSUER_EVENTS,
    _PRE_MOVE_TIMINGS,
    AboutTicker,
    EventCluster,
    EventType,
    GateConfig,
    GateDecision,
    _as_utc,
    _classifier_quote_trace,
    _group_explains_move,
    _pairing_rule_outcome,
    _same_direction,
)

STALE_REASON = "STALE_PRIMARY"


def _band(observation, key: str) -> str:
    value = observation.get(key) if isinstance(observation, dict) else getattr(observation, key, None)
    raw = value.get("band") if isinstance(value, dict) else getattr(value, "band", None)
    return str(getattr(raw, "value", raw) or "").upper()


def select_and_gate(clusters: list[EventCluster], facts: dict, config: GateConfig, *,
                    stale=lambda c: False, extra_ok=lambda c: True, stale_counts: bool = False) -> GateDecision:
    """Rank by the documented lexicographic score; earliest eligible source breaks ties.

    The desk variant is standard plus two inference paths a desk analyst uses when no source states the
    link: a confirmed material company event timed before or during the session, and a same-session
    group driver for a stock that moved with that group. Reported attributions always rank first.
    """
    observation = facts.get("observation", facts)
    market = _same_direction(observation.get("market_comove") if isinstance(observation, dict) else getattr(observation, "market_comove", None))
    sector = _same_direction(observation.get("sector_comove") if isinstance(observation, dict) else getattr(observation, "sector_comove", None))
    idiosyncratic = not (market or sector)
    peers_aligned = _band(observation, "peer_comove") == "ALIGNED"
    m3 = config.variant == "desk_m3"
    decomposition = facts.get("decomposition") if m3 else None
    verdict = getattr(decomposition, "verdict", "UNAVAILABLE")
    best_kind = getattr(getattr(decomposition, "best", None), "kind", None)
    if m3 and verdict != "UNAVAILABLE":
        # The beta decomposition replaces the size-only comovement band (M3).
        idiosyncratic = verdict != "GROUP_DRIVEN"
    eligible: list[tuple[EventCluster, str]] = []
    diagnostics = []
    # v2 hook: a stale cluster (its event happened before the window) is not a session event at all.
    issuer_events = sum(1 for c in clusters if c.scope == AboutTicker.ISSUER and c.this_session
                        and (stale_counts or not stale(c))
                        and c.event_type in _MATERIAL_ISSUER_EVENTS)
    for cluster in clusters:
        group_ok = (cluster.scope != AboutTicker.GROUP or market or sector
                    or (m3 and verdict in ("GROUP_DRIVEN", "MIXED")))
        has_event = bool(cluster.best_event or cluster.best_attribution)
        if config.variant == "strict":
            threshold = cluster.explicit_origins >= 2 or (cluster.explicit_origins >= 1 and cluster.has_primary_source)
        elif config.variant in ("standard", "desk", "desk_m3"):
            threshold = cluster.explicit_origins >= 1
        else:
            threshold = has_event
        basis = "REPORTED_ATTRIBUTION" if cluster.explicit_origins >= 1 else None
        if config.variant in ("desk", "desk_m3") and cluster.scope == AboutTicker.GROUP:
            # A group driver needs a source saying why the market/sector/commodity moved (explicit
            # attribution at group level) and a stock that tracked that group. "The market fell" or a
            # stale company fact is not a driver (v6 dev: FCX, DIS).
            explains = (verdict in ("GROUP_DRIVEN", "MIXED") if m3 and verdict != "UNAVAILABLE"
                        else _group_explains_move(cluster.event_type, observation))
            stated = threshold and cluster.event_type in _GROUP_EVENTS and explains
            basis = "GROUP_DRIVER" if stated else None
            # M3: the decomposition says the group explains most of the move and a same-session event
            # about that kind of group is in the evidence, though no source states the link.
            inferred = (m3 and not stated and verdict == "GROUP_DRIVEN" and cluster.this_session and has_event
                        and cluster.event_type in _GROUP_EVENTS_FOR.get(best_kind, frozenset()))
            threshold = stated or inferred
            if inferred:
                basis = "GROUP_MOVE_INFERRED"
        if config.variant in ("desk", "desk_m3") and not threshold and group_ok and has_event and cluster.this_session:
            # A source published before the close proves the event preceded (or accompanied) the move.
            close = _as_utc(facts.get("session_close"))
            timed = bool(cluster.timings & _PRE_MOVE_TIMINGS) or (close is not None and cluster.earliest <= close)
            # With no source stating the link, a confirmed company event is inferred as the cause only when
            # it is the session's only company event (v6 dev: MDT FDA filing vs BTIG upgrade, PYPL CEO
            # change vs Stripe report).
            # 2026-11-16: nor when the stock moved in line with its peers: the group already explains the
            # move (ORCL 07-14 fell 2.7% with peers at -3.2% and an AI product launch was inferred as the cause).
            if (cluster.scope == AboutTicker.ISSUER and cluster.event_type in _MATERIAL_ISSUER_EVENTS and timed
                    and (cluster.has_primary_source or cluster.independent_origins >= 2)
                    and issuer_events == 1 and not peers_aligned):
                threshold, basis = True, "CONFIRMED_EVENT_INFERRED_LINK"
        pairing_rule = _pairing_rule_outcome(cluster, enabled=config.variant in ("desk", "desk_m3"))
        blocked_pairing = pairing_rule == "rejected_lead_same_source_event"
        allowed = group_ok and has_event and threshold and not blocked_pairing
        v2_blocked = allowed and (stale(cluster) or not extra_ok(cluster))  # v2 hooks: eligibility only
        allowed = allowed and not v2_blocked
        diagnostics.append({"event_type": cluster.event_type.value, "scope": cluster.scope.value,
                            "independent_origins": cluster.independent_origins, "explicit_origins": cluster.explicit_origins,
                            "has_primary_source": cluster.has_primary_source, "has_event_quote": has_event,
                            "group_consistent": group_ok, "this_session": cluster.this_session,
                            "basis": basis if allowed else None, "eligible": allowed,
                            "best_attribution": _classifier_quote_trace(
                                cluster.best_attribution, cluster.best_attribution_roles),
                            "best_event": _classifier_quote_trace(cluster.best_event, cluster.best_event_roles),
                            "pairing_rule": pairing_rule,
                            **({"v2_blocked": True} if v2_blocked else {})})
        if allowed:
            eligible.append((cluster, basis or "NOGATE"))
    basis_rank = {"REPORTED_ATTRIBUTION": 0, "GROUP_DRIVER": 0, "GROUP_MOVE_INFERRED": 1,
                  "CONFIRMED_EVENT_INFERRED_LINK": 1, "NOGATE": 3}
    if m3 and verdict == "GROUP_DRIVEN":
        # A desk leads with the group when beta explains most of the move (v6 dev: AVGO's sector rebound
        # beat a same-day analyst note; BAC/BA rose with their sectors on the ceasefire).
        basis_rank = {"GROUP_DRIVER": 0, "GROUP_MOVE_INFERRED": 1, "REPORTED_ATTRIBUTION": 2,
                      "CONFIRMED_EVENT_INFERRED_LINK": 3, "NOGATE": 4}
    def rank(pair: tuple[EventCluster, str]) -> tuple:
        c, basis = pair
        scope_rank = (0 if c.scope == AboutTicker.ISSUER else 1) if idiosyncratic else (0 if c.scope == AboutTicker.GROUP else 1)
        return (basis_rank[basis], -c.explicit_origins, -int(c.has_primary_source), -c.independent_origins, scope_rank, c.earliest)
    eligible.sort(key=rank)
    if not eligible:
        return GateDecision(False, None, None, "NO_ELIGIBLE_CAUSE", tuple(diagnostics))
    top, top_basis = eligible[0]
    if len(eligible) > 1:
        second, second_basis = eligible[1]
        tied = (top.explicit_origins, top.has_primary_source) == (second.explicit_origins, second.has_primary_source)
        competing = tied and top.scope == second.scope == AboutTicker.ISSUER and top.event_type != second.event_type
        if competing and (config.variant == "strict"
                          or (config.variant == "desk" and top_basis == second_basis == "CONFIRMED_EVENT_INFERRED_LINK")):
            # Two confirmed company events and no source saying which one moved the stock.
            return GateDecision(False, None, None, "COMPETING_CAUSES", tuple(diagnostics))
    secondary = next((c for c, _ in eligible[1:] if c.explicit_origins >= 1), None)
    return GateDecision(True, top, secondary, "PUBLISHED", tuple(diagnostics), top_basis)


def gate(clusters: list[EventCluster], facts: dict, *, veto: Callable[[EventCluster], bool] | None = None
         ) -> GateDecision:
    """The frozen desk gate; with ``veto``, a stale top cluster makes the stage abstain (reason STALE_PRIMARY)."""
    decision = select_and_gate(clusters, facts, GateConfig("desk"))
    if veto is not None and decision.publish and veto(decision.primary):
        return GateDecision(False, None, None, STALE_REASON, decision.diagnostics)
    return decision
