"""Peers' same-day earnings: which of a stock's peers had an 8-K listing Item 2.02 accepted by the SEC in the
session window (previous trading day 16:00 ET, session day 16:00 ET]. A [data] fact for the market-facts line
("peer X reported the evening before"), never a cause on its own; wiring it in is a B-class change. The query needs
no fetching: the SEC submissions copies already in the store directory (the `recent` segment).

Read-only study 9c (2026-10-22, scripts/research/v3/peer_earnings.py on v2/implementation) used the same rule.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from catalyst_data.ingestion.time import timefields
from catalyst_data.trading_calendar import calendar_trading_days

NY = timefields.NY


def window(day: str) -> tuple[datetime, datetime]:
    prev = [d for d in calendar_trading_days((date.fromisoformat(day) - timedelta(days=7)).isoformat(), day)
            if d < day][-1]
    close = lambda d: datetime(*map(int, d.split("-")), 16, tzinfo=NY).astimezone(timezone.utc)  # noqa: E731
    return close(prev), close(day)


def earnings_acceptances(submissions_dir: Path, cik: int) -> list[datetime] | None:
    """Acceptance times of the issuer's 8-Ks listing Item 2.02; None when there is no local submissions copy."""
    path = Path(submissions_dir) / f"{cik:010d}.json"
    if not path.is_file():
        return None
    r = json.loads(path.read_text())["filings"]["recent"]
    # the JSON time (flag "unreliable": 4-5 h late for about 60% of issuers); a correction table can be applied
    times = [timefields.external(a, source="sec", field="acceptanceDateTime").utc
             for f, i, a in zip(r["form"], r["items"], r["acceptanceDateTime"])
             if f.startswith("8-K") and "2.02" in (i or "").split(",")]
    return [t for t in times if t is not None]


def peer_earnings(peers, day: str, submissions_dir: Path, cik_by_ticker: dict[str, int]) -> dict:
    """{"reported": [(peer, acceptance UTC ISO)], "not_covered": [peers without a submissions copy]}."""
    start, end = window(day)
    reported, missing = [], []
    for p in peers:
        times = earnings_acceptances(submissions_dir, cik_by_ticker[p]) if p in cik_by_ticker else None
        if times is None:
            missing.append(p)
            continue
        reported += [(p, t.strftime("%Y-%m-%dT%H:%M:%SZ")) for t in sorted(times) if start < t <= end]
    return {"reported": reported, "not_covered": missing}
