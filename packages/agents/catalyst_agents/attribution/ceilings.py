"""Honesty ceilings applied to a PRIMARY cause after the quote rules.

Co-movement, subject and pre-open timing gates; each can only lower the
status ceiling to ABSTAIN, never raise it. Moved verbatim from assessment.py (P3).
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from catalyst_agents.attribution.analyst import AttributionStatus, HypothesisRole

if TYPE_CHECKING:
    from catalyst_agents.attribution.assessment import (
        ContextPackSurface,
        NormalizedHypothesis,
    )


def _inventory_index(context_pack: ContextPackSurface) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for item in context_pack.evidence_inventory:
        evidence_id = getattr(item, "evidence_id", None)
        if evidence_id:
            index[evidence_id] = item
    return index


def _comove_ceiling(
    hypotheses: tuple[NormalizedHypothesis, ...], observation: Any
) -> tuple[AttributionStatus | None, str]:
    """Step 2 co-movement check on a sector- or market-driven PRIMARY.

    A sector or market driver explains the move only if the stock actually
    moved with it that session (the locked one-session alignment bands).
    SECTOR_MOVE needs the sector ETF or the peers ALIGNED; MACRO_EVENT needs
    the market, the sector, the peers or the commodity proxy to have moved the
    same way. A driver every available reference moved *against* cannot be
    the cause (ABSTAIN); anything else passes. Unknown or unavailable
    references never lower the ceiling.
    """
    primary = next(
        (h for h in hypotheses if h.normalized_role is HypothesisRole.PRIMARY), None
    )
    if primary is None or observation is None:
        return None, ""
    cause = primary.cause_type.value
    if cause not in ("SECTOR_MOVE", "MACRO_EVENT"):
        return None, ""
    names = ("sector_comove", "peer_comove")
    if cause == "MACRO_EVENT":
        names = ("market_comove",) + names
    bands = [
        getattr(getattr(observation, name, None), "band", None) for name in names
    ]
    relations = [
        getattr(getattr(observation, name, None), "direction_relation", None)
        for name in names
    ]
    if cause == "MACRO_EVENT":
        commodity_return = getattr(observation, "commodity_return", None)
        target_return = getattr(observation, "target_return", None)
        if (
            commodity_return is not None and target_return is not None
            and abs(commodity_return) >= 1.0 and (commodity_return > 0) == (target_return > 0)
        ):
            return None, ""
    known = [b for b in bands if b is not None and b.value in ("ALIGNED", "DIVERGENT")]
    if not known or any(b.value == "ALIGNED" for b in known):
        return None, ""
    if all(
        r is not None and r.value == "OPPOSITE_DIRECTION"
        for b, r in zip(bands, relations)
        if b is not None and b.value == "DIVERGENT"
    ):
        return AttributionStatus.ABSTAIN, f"comove_opposite:{cause}"
    return None, ""


_RENDERED_QUOTE = re.compile(r"“(.*?)” — ")


_GATE_GROUP = r"(?:stocks?|shares|sector|names|group|index|etfs?|segment|industry|space|companies|peers)"


def _stored_titles(evidence_ids, path) -> dict[str, str]:
    """Article titles of attached web-body chunks (chunk id = evidence id) from the old body store.

    A store that cannot be read raises: an empty result here would silently turn the subject gate's title rule
    off (it used to return {} on any sqlite error)."""
    import sqlite3

    ids = [eid for eid in evidence_ids if ":web_body:" in eid]
    if not ids:
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        marks = ",".join("?" for _ in ids)
        return dict(conn.execute(
            f"SELECT chunk_id, title FROM web_body_chunks WHERE chunk_id IN ({marks})", ids
        ).fetchall())
    except sqlite3.Error as exc:
        from catalyst_data.storage.errors import StoreReadError

        raise StoreReadError(path, "read web body titles", str(exc)) from exc
    finally:
        if "conn" in locals():
            conn.close()


def _title_source():
    """Where stored titles come from: a callable ``ids -> {chunk id: title}``, or None when no body store is
    configured (no CATALYST_NEWS_BODIES_DB: the title rule stays off, as before).

    A configured legacy cohort is checked first and replaces the old table row for row; the env var may then
    name a store file that is absent (it only locates the company-name map). Without a cohort the old store must
    exist where the env var says, else this raises rather than silently dropping titles.
    """
    from catalyst_data.retrieval import legacy_read
    from catalyst_data.retrieval.web_bodies import ENV_VAR, name_map_path, store_path

    cohort = legacy_read.configured_reader()
    if cohort is not None:
        return lambda ids: cohort.titles_for_chunks([eid for eid in ids if ":web_body:" in eid])
    if name_map_path() is None:
        return None
    path = store_path()
    if path is None:
        from catalyst_data.storage.errors import StoreReadError

        raise StoreReadError(
            ENV_VAR, "open web body title store",
            "configured body store does not exist and no legacy cohort is set",
        )
    return lambda ids: _stored_titles(ids, path)


def _subject_ceiling(
    hypotheses: tuple[NormalizedHypothesis, ...], context_pack: ContextPackSurface
) -> tuple[AttributionStatus | None, str]:
    """Step 3 subject gate: a PRIMARY must be about the target.

    At least one published quote, from a non-opinion item (by URL), must name the
    target company, or, for a sector, market or commodity driver, the group
    (sector terms, the commodity, the US market). A quote that names only
    another company (Halliburton for SLB, Verizon for TMUS) does not explain
    the target, so the answer abstains.
    """
    primary = next(
        (h for h in hypotheses if h.normalized_role is HypothesisRole.PRIMARY), None
    )
    if primary is None:
        return None, ""
    observation = getattr(context_pack, "observation", None)
    ticker = getattr(observation, "ticker", None)
    if not ticker:
        return None, ""

    from catalyst_data.retrieval.web_bodies import (
        _GROUP,
        SECTOR_TERMS,
        name_map_path,
        subject_matcher,
    )

    # Without the SEC name map only the ticker is known ("Apple" would not
    # name AAPL), so the gate stays off rather than abstain on a guess.
    titles_of = _title_source()  # raises when a configured store is missing and no cohort is set
    name_map = name_map_path()
    if titles_of is None or name_map is None or not name_map.is_file():
        return None, ""

    cause = primary.cause_type.value
    targets = [subject_matcher(ticker)]
    if cause in ("SECTOR_MOVE", "MACRO_EVENT", "COMMODITY"):
        group_tickers = [getattr(observation, "sector_ticker", None)]
        if group_tickers[0] == "XLK":
            group_tickers.append("SMH")
        group_tickers.append(getattr(observation, "commodity_ticker", None))
        if cause == "MACRO_EVENT":
            group_tickers.append("SPY")
        for group_ticker in filter(None, group_tickers):
            targets.append(subject_matcher(group_ticker))
            if group_ticker in SECTOR_TERMS:
                # A quote may name the group as a segment or industry ("the
                # entire enterprise software segment was down"); retrieval
                # keeps its narrower phrases.
                wide = re.compile(
                    r"\b(?:" + "|".join(
                        term.replace(_GROUP, _GATE_GROUP)
                        for term in SECTOR_TERMS[group_ticker]
                    ) + r")\b",
                    re.IGNORECASE,
                )
                targets.append(wide.search)

    from catalyst_agents.attribution.quotes import _match_key

    inventory = _inventory_index(context_pack)
    # (excerpt key, the article is about the target): an issuer's own filing,
    # or a stored article whose title names the target (the intake subject
    # rule), so "The U.S. large pharma company reported..." in a Bristol
    # Myers article counts, while a Halliburton article does not for SLB.
    titles = titles_of(primary.supporting_evidence_ids)
    sources = []
    for evidence_id in primary.supporting_evidence_ids:
        item = inventory.get(evidence_id)
        if item is None:
            continue
        # Opinion columns (an AMT-vs-CCI comparison) name the company without
        # reporting a cause. Publisher-level "analysis" classes are not used:
        # a "why X stock fell today" report is news.
        url = (getattr(item, "canonical_url", None) or "").lower()
        if "/opinion/" in url:
            continue
        source_class = getattr(item.source_class, "value", item.source_class)
        about = (
            source_class == "issuer_disclosure"
            or "sec.gov/" in url
            or targets[0](titles.get(evidence_id, ""))
        )
        sources.append((_match_key(getattr(item, "excerpt_text", None) or ""), about))
    # The statement is code-rendered quotes: “quote” — publisher, time, url.
    for quote in _RENDERED_QUOTE.findall(primary.statement):
        key = _match_key(quote)
        for source, about in sources:
            if key and key in source and (about or any(m(quote) for m in targets)):
                return None, ""
    return AttributionStatus.ABSTAIN, f"primary_off_subject:{cause}"


# Step 4 timing check: share of the day's move that came at the open.
PRE_OPEN_MAX_GAP_SHARE = 0.15


PRE_OPEN_MIN_MOVE_PCT = 2.0


def _timing_ceiling(
    hypotheses: tuple[NormalizedHypothesis, ...], context_pack: ContextPackSurface
) -> tuple[AttributionStatus | None, str]:
    """A story published before the open cannot explain an intraday move.

    News public before the open is priced at the open. When every item behind
    the PRIMARY was eligible before the session opened, yet the stock opened
    almost flat (the opening gap is under 15% of a >= 2% day) and made its
    move during the session, the PRIMARY's story is not shown to be the cause
    (a pre-open preview, a weekend recap, brand history): ABSTAIN.
    """
    primary = next(
        (h for h in hypotheses if h.normalized_role is HypothesisRole.PRIMARY), None
    )
    observation = getattr(context_pack, "observation", None)
    temporal = getattr(context_pack, "temporal_identity", None)
    if primary is None or observation is None or temporal is None:
        return None, ""
    gap = getattr(observation, "gap_return", None)
    total = getattr(observation, "target_return", None)
    if gap is None or total is None or abs(total) < PRE_OPEN_MIN_MOVE_PCT:
        return None, ""
    if abs(gap) >= PRE_OPEN_MAX_GAP_SHARE * abs(total):
        return None, ""
    inventory = _inventory_index(context_pack)
    times = [
        inventory[eid].eligible_at
        for eid in primary.supporting_evidence_ids
        if eid in inventory
    ]
    if not times or any(t >= temporal.session_open_at for t in times):
        return None, ""
    return AttributionStatus.ABSTAIN, f"primary_pre_open_intraday_move:{primary.cause_type.value}"
