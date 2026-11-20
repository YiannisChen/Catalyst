"""Code-computed session facts shown with the observed move (facts panel).

Co-movement verdicts, commodity direction, opening-gap timing and in-window
issuer filings. Facts are context, never a cause. Moved verbatim from graph.py (P3).
"""
from __future__ import annotations

from typing import Any

from catalyst_agents.attribution.move_profile import MoveProfile


def _observed_move_text(ticker: str, move_profile: MoveProfile, session_date: str) -> str:
    target_return = getattr(move_profile, "target_return", None)
    if target_return is None:
        return f"{ticker} moved on {session_date}"
    text = f"{ticker} {target_return:+.2f}% on {session_date}"
    # M9 v2: code-owned market / sector / peer context, when available.
    context = []
    market = getattr(move_profile, "market_comove", None)
    if market is not None and market.reference_return_pct is not None:
        context.append(f"market {market.reference_return_pct:+.2f}%")
    sector = getattr(move_profile, "sector_comove", None)
    if sector is not None and sector.reference_return_pct is not None:
        etf = getattr(move_profile, "sector_ticker", None)
        context.append(f"sector ETF {etf + ' ' if etf else ''}{sector.reference_return_pct:+.2f}%")
    peers = getattr(move_profile, "peer_summary", None)
    if peers is not None and peers.median_return_pct is not None:
        context.append(
            f"peer median {peers.median_return_pct:+.2f}% "
            f"across {peers.available_peer_count} peers"
        )
    commodity = getattr(move_profile, "commodity_ticker", None)
    commodity_return = getattr(move_profile, "commodity_return", None)
    if commodity and commodity_return is not None:
        context.append(f"commodity proxy {commodity} {commodity_return:+.2f}%")
    if context:
        text += "; " + ", ".join(context)
    return text


_COMOVE_REFERENCES = (
    ("market_comove", "market"),
    ("sector_comove", "sector"),
    ("peer_comove", "peers"),
)


def _session_facts(move_profile: MoveProfile, context_pack: Any, temporal_identity: Any) -> str:
    """Code-computed session facts shown with the observed move (never a cause).

    The co-movement verdict restates the locked one-session alignment bands in
    words, and the issuer's SEC filings accepted inside the session window are
    listed with their time and link, so an answer without a verified cause
    still says what can be checked.
    """
    verdicts = []
    for field, name in _COMOVE_REFERENCES:
        alignment = getattr(move_profile, field, None)
        band = getattr(getattr(alignment, "band", None), "value", None)
        relation = getattr(getattr(alignment, "direction_relation", None), "value", None)
        if band == "ALIGNED":
            verdicts.append(f"in line with the {name}")
        elif band == "DIVERGENT" and relation == "OPPOSITE_DIRECTION":
            verdicts.append(f"against the {name}")
        elif band == "DIVERGENT" and relation == "SAME_DIRECTION":
            target = abs(alignment.target_return_pct or 0.0)
            reference = abs(alignment.reference_return_pct or 0.0)
            size = "larger" if target > reference else "smaller"
            verdicts.append(f"same direction as the {name} but much {size}")
    # The commodity proxy has no alignment band: direction only, on a >= 1% move.
    commodity = getattr(move_profile, "commodity_ticker", None)
    commodity_return = getattr(move_profile, "commodity_return", None)
    target_return = getattr(move_profile, "target_return", None)
    if commodity and commodity_return is not None and target_return and abs(commodity_return) >= 1.0:
        same = (commodity_return > 0) == (target_return > 0)
        verdicts.append(f"{'with' if same else 'against'} the commodity proxy {commodity}")
    facts = []
    if verdicts:
        facts.append("Co-movement (computed from closing prices): " + "; ".join(verdicts) + ".")
    # When the move happened: the opening gap prices news from before the open,
    # the open-to-close part what happened during the session.
    gap = getattr(move_profile, "gap_return", None)
    if gap is not None and target_return is not None and gap > -100.0:
        session = ((1 + target_return / 100) / (1 + gap / 100) - 1) * 100
        facts.append(f"Timing: opening gap {gap:+.2f}%, open to close {session:+.2f}%.")
    filings, seen = [], set()
    for item in getattr(context_pack, "evidence_inventory", ()) or ():
        url = getattr(item, "canonical_url", None) or ""
        if "sec.gov/" not in url or url in seen:
            continue
        if not (
            temporal_identity.information_window_start_at
            < item.eligible_at
            <= temporal_identity.cutoff_at
        ):
            continue
        seen.add(url)
        filings.append((item.eligible_at, url))
    for when, url in sorted(filings)[:3]:
        facts.append(f"SEC filing accepted {when.strftime('%Y-%m-%d %H:%M UTC')}: {url}.")
    return " ".join(facts)
