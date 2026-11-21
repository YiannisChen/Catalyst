"""Rule 3: the earnings-filing fallback.

After both stages abstain, the issuer's own results release is the answer when all hold:
- the tier is T3;
- an Item 2.02 8-K was accepted after the prior close and no later than D's close;
- the research residual |z| >= 2 (catalyst_data.ingestion.groups._market_z, the prospective sampling model); no z, no fallback;
- no other material 8-K (items below) was accepted in the window on another filing;
- that filing's exhibit is in the evidence pack (it is quoted from there, so the quote rules can check it).
The quote is the release's own summary sentence (first_sentence); the answer is PARTIAL with basis EARNINGS_FILING.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

MATERIAL_ITEMS = frozenset({"1.01", "1.02", "2.01", "2.05", "2.06", "3.01", "5.01", "5.02", "8.01"})
Z_MIN = 2.0
MIN_PARAGRAPH = 40
BASIS = "EARNINGS_FILING"


def first_sentence(text: str) -> str:
    """The release's own summary, verbatim: within the first six paragraphs of at least 40 characters (contact lines
    skipped, a leading bullet dropped), the first saying the company "today reported/announced", else a
    "... Reports ... Results" headline, else the first paragraph; at most 400 characters, cut after the last "; " or
    ". " inside the limit (the rule of the prospective fourth configuration)."""
    paragraphs = [" ".join(p.split()).lstrip("•◦▪–-* ") for p in text.split("\n\n")]
    paragraphs = [p for p in paragraphs if len(p) >= MIN_PARAGRAPH
                  and not re.match(r"(contacts?|media|investor relations|for further)\b", p, re.I)][:6] \
        or [" ".join(text.split())]
    lead = (next((p for p in paragraphs if re.search(r"\btoday\b.{0,80}\b(report|announc|post)", p, re.I)), None)
            or next((p for p in paragraphs if re.search(r"\b(reports?|announces?)\b.*\bresults\b", p, re.I)), None)
            or paragraphs[0])
    if len(lead) <= 400:
        return lead
    cut = max(lead.rfind("; ", 0, 400), lead.rfind(". ", 0, 400))
    return lead[:cut + 1] if cut >= MIN_PARAGRAPH else lead[:400]


def _accession_digits(accession: str) -> str:
    return accession.replace("-", "")


ITEM_CAPTIONS = {
    "1.01": "Entry into a Material Definitive Agreement", "1.02": "Termination of a Material Definitive Agreement",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.05": "Costs Associated with Exit or Disposal Activities", "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Continued Listing Rule",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure or Appointment of Directors or Officers; Compensatory Arrangements",
    "5.07": "Submission of Matters to a Vote of Security Holders", "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events", "9.01": "Financial Statements and Exhibits",
}
FACT_LABEL = "Filing fact: "


def filing_fact(company: str, ticker: str, filing: dict) -> str:
    """v2.1 (rule ``filing``): the conclusion's code-rendered fact sentence. Every field is SEC metadata the caller
    verified against the submissions index (company, form, items, acceptance time); nothing comes from a model."""
    from zoneinfo import ZoneInfo
    at = filing["accepted_at"]
    at = datetime.fromisoformat(at) if isinstance(at, str) else at
    when = at.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
    items = "; ".join(f"Item {i} {ITEM_CAPTIONS[i]}" if i in ITEM_CAPTIONS else f"Item {i}"
                      for i in filing["items"])
    return (f"{FACT_LABEL}{company} ({ticker}) filed a Form {filing.get('form') or '8-K'} with the SEC, accepted "
            f"{when} (accession {filing['accession']}), reporting {items}.")


def decide(*, tier: str | None, filings: list[dict], z: float | None, prior_close: datetime, close: datetime,
           pack_items: list[dict], title_for=None) -> dict:
    """{"fire": bool, "reason": str, "filing": ..., "item": ..., "quote": ...} for the fallback (pure)."""
    window = [f for f in filings if prior_close < f["accepted_at"] <= close]
    earnings = [f for f in window if "2.02" in f["items"]]
    if tier != "T3":
        return {"fire": False, "reason": "tier_not_T3"}
    if not earnings:
        return {"fire": False, "reason": "no_item_202"}
    if z is None:
        return {"fire": False, "reason": "no_research_z"}
    if abs(z) < Z_MIN:
        return {"fire": False, "reason": "z_below_2", "z": z}
    accessions = {f["accession"] for f in earnings}
    others = [f["accession"] for f in window if f["accession"] not in accessions and set(f["items"]) & MATERIAL_ITEMS]
    if others:
        return {"fire": False, "reason": "other_material_8k", "other": others}
    first = min(earnings, key=lambda f: f["accepted_at"])
    digits = _accession_digits(first["accession"])
    matches = [i for i in pack_items if "sec.gov" in str(i.get("canonical_url") or "")
               and digits in str(i.get("canonical_url") or "").replace("-", "")]
    if not matches:
        return {"fire": False, "reason": "exhibit_not_in_pack", "accession": first["accession"]}
    item = next((i for i in matches if re.search(r"ex-?99|exhibit99", str(i.get("canonical_url")), re.I)), matches[0])
    excerpt = str(item.get("excerpt_text") or "")
    quote, unit = first_sentence(excerpt), "first_sentence"
    if title_for is not None:  # v2.1: the release headline, quoted as a title unit, when it is in the pack's excerpt
        from catalyst_agents.attribution.quotes import _match_key
        title = title_for(item.get("canonical_url"))
        if title and _match_key(title) in _match_key(excerpt):
            quote, unit = title, "title"
    if not quote or quote not in " ".join(excerpt.split()):
        return {"fire": False, "reason": "no_verbatim_quote"}
    return {"fire": True, "reason": "fired", "z": z, "accession": first["accession"],
            "accepted_at": first["accepted_at"].isoformat(), "evidence_id": item.get("evidence_id"), "quote": quote,
            "quote_unit": unit, "items": list(first["items"]), "form": first.get("form")}


def answer_records(item: dict, quote: str) -> list:
    """The single record the fallback publishes: the exhibit with the release sentence as its event quote."""
    from catalyst_agents.attribution.extraction import (
        AboutTicker,
        EventTiming,
        EventType,
        Extraction,
        Session,
    )
    ext = Extraction(about_ticker=AboutTicker.ISSUER, session=Session.THIS_SESSION, event_type=EventType.EARNINGS,
                     event_summary="issuer results release (Item 2.02)", event_timing=EventTiming.UNKNOWN,
                     explicit_attribution=False, attribution_quote="", event_quote=quote, mechanism_quote="",
                     is_preview_or_evergreen=False)
    return [(item, ext)]


def research_z(market_db: Any, ticker: str, session_date: str) -> float | None:
    import sqlite3
    from pathlib import Path

    from catalyst_data.ingestion.groups import _market_z
    from catalyst_data.ingestion.movers import _matrix
    from catalyst_data.storage.errors import StoreReadError

    path = Path(market_db).expanduser()
    if not path.is_file():
        cause = FileNotFoundError(path)
        raise StoreReadError(path, "read earnings research market returns", str(cause)) from cause
    try:
        _, ret, _ = _matrix(path, ["SPY", ticker], session_date)
    except sqlite3.Error as exc:
        raise StoreReadError(path, "read earnings research market returns", str(exc)) from exc
    except ValueError:
        # A readable market store with no SPY bar for this session is ordinary
        # absence, distinct from a missing or unreadable store.
        return None
    z = _market_z(ret[:, 1], ret[:, 0])
    return None if z is None else float(z)
