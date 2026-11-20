"""v2 switches. CATALYST_V2_RULES is a comma list; empty (the default) reproduces A′ exactly.

  recency   rule 1: a stale top cluster ends the stage (stages 1 and 2); needs event dates (event_date_v1)
  anchor    rule 2: an issuer EARNINGS cluster is dated by the issuer's Item 2.02 acceptance (needs recency)
  earnings  rule 3: the earnings-filing fallback after both stages abstain
  window    rule 4: the stage-2 window starts at the prior close (D-1 close .. D+3) instead of D-4's close
  filing    v2.1: SEC exhibits are checked by their own units (title, bullet, paragraph; news is unchanged), and
            the earnings fallback answers with the filing fact (verified SEC metadata) and the release headline
  v2        the four v2 rules (recency, anchor, earnings, window), i.e. backend-freeze-v2
  all       every rule above
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

NAMES = ("recency", "anchor", "earnings", "window", "filing")
V2 = ("recency", "anchor", "earnings", "window")


@dataclass(frozen=True)
class V2Rules:
    recency: bool = False
    anchor: bool = False
    earnings: bool = False
    window: bool = False
    filing: bool = False

    @classmethod
    def parse(cls, text: str | None) -> "V2Rules":
        names = {n.strip() for n in (text or "").split(",") if n.strip()}
        if "all" in names:
            names = set(NAMES)
        if "v2" in names:
            names = (names - {"v2"}) | set(V2)
        unknown = names - set(NAMES)
        if unknown:
            raise ValueError(f"unknown v2 rules: {sorted(unknown)}")
        if "anchor" in names and "recency" not in names:
            raise ValueError("the earnings anchor is part of the recency rule")
        return cls(**{n: n in names for n in NAMES})

    @property
    def any(self) -> bool:
        return any(asdict(self).values())

    def to_dict(self) -> dict:
        return asdict(self)
