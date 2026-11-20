"""Read-only mover selection and supported daily universe access."""
from __future__ import annotations

import json
from pathlib import Path

from catalyst_data.read import market_scope
from catalyst_data.storage import runtime_paths
from catalyst_data.storage.connect import connect_readonly

MOVER_MIN_ABS_RETURN_PCT = 2.0


def universe() -> list[str]:
    """Return sorted tickers from the held-out universe manifest."""
    names: set[str] = set()
    manifest = runtime_paths.repository_root() / "data/manifests/heldout_universe_v1.json"
    for members in json.loads(manifest.read_text())["sectors"].values():
        names.update(members)
    return sorted(names)


def movers_for(market_db: Path, session_date: str, tickers: list[str]) -> list[dict]:
    """Return tickers whose read-only total return meets the daily mover threshold."""
    with connect_readonly(market_db, immutable=False, timeout_ms=10_000) as conn:
        out = []
        for ticker in tickers:
            value = market_scope.session_return(conn, ticker, session_date)
            if value is not None and abs(value) >= MOVER_MIN_ABS_RETURN_PCT:
                out.append({"ticker": ticker, "return_pct": round(value, 3)})
    return sorted(out, key=lambda row: -abs(row["return_pct"]))
