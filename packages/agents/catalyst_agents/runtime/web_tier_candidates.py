"""CLI adapter that returns daily T3 movers without coupling data-core to agents."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def _candidate_row(mover: dict, decision, profile, *, all_tiers: bool) -> dict | None:
    if all_tiers:
        return {"ticker": mover["ticker"], "tier": decision.tier}
    if decision.tier != "T3":
        return None
    reference = decision.governing_ref or {}
    residual = reference.get("residual_pct")
    return {"ticker": mover["ticker"],
            "priority": abs(float(residual if residual is not None else profile.target_return or 0.0))}


def candidates_for_session(*, market_db: Path, snapshot_db: Path | None,
                           peer_map: Path, session_date: str,
                           all_tiers: bool = False) -> list[dict]:
    from catalyst_data.canonical.temporal import build_temporal_identity, utc_iso_z
    from catalyst_data.read.daily_universe import movers_for, universe

    from catalyst_agents.attribution.observation_builder import ObservationBuilder
    from catalyst_agents.attribution.tiers import (
        decide_tier,
        default_observation_policy,
    )
    from catalyst_agents.runtime.context import SQLiteContextProvider

    os.environ["CATALYST_MARKET_CONTEXT_DB"] = str(market_db)
    os.environ["CATALYST_PEER_MAP"] = str(peer_map)
    provider = SQLiteContextProvider(snapshot_db or market_db)
    policy = default_observation_policy()
    builder = ObservationBuilder(provider=provider, policy=policy)
    identity = build_temporal_identity(session_date)
    result = []
    for mover in movers_for(market_db, session_date, universe()):
        profile = builder.build(ticker=mover["ticker"], session_date=session_date,
                                cutoff=utc_iso_z(identity.cutoff_at), temporal_identity=identity)
        try:
            filings = provider.issuer_filings_in_window(
                ticker=mover["ticker"],
                information_window_start_at=utc_iso_z(identity.information_window_start_at),
                cutoff_at=utc_iso_z(identity.cutoff_at))
        except Exception:
            filings = 0
        decision = decide_tier(profile, filings_in_window=filings, policy=policy)
        candidate = _candidate_row(mover, decision, profile, all_tiers=all_tiers)
        if candidate is not None:
            result.append(candidate)
    if all_tiers:
        return sorted(result, key=lambda item: item["ticker"])
    return sorted(result, key=lambda item: (-item["priority"], item["ticker"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-db", required=True, type=Path)
    parser.add_argument("--snapshot-db", type=Path)
    parser.add_argument("--peer-map", required=True, type=Path)
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--all-tiers", action="store_true")
    args = parser.parse_args()
    print(json.dumps(candidates_for_session(market_db=args.market_db,
                                            snapshot_db=args.snapshot_db,
                                            peer_map=args.peer_map,
                                            session_date=args.session_date,
                                            all_tiers=args.all_tiers)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
