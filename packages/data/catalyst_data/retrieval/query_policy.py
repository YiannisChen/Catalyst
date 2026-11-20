"""Deterministic lexical query policy for attribution queries (AMEND-5.1).

Realistic attribution questions look like:

    Why did TSLA move -8.2% on 2025-07-24?

Raw tokenization would AND-join every stopword, ticker, digit and date part
and silently return zero.  This module instead:

1. keeps the eligibility predicate (ticker/date/manifest/status/cutoff) as the
   only mandatory structure — ticker/percentage/date tokens never enter the
   mandatory MATCH clause;
2. always treats the structured ``ticker`` argument as recognized (removed
   from content terms), for every symbol ticker including the full
   ``RATIFIED_TICKERS`` universe;
3. removes stopwords, month names, pure-number tokens and issuer aliases
   derived from the versioned universe legal-name metadata;
4. enriches with issuer name tokens from the same authoritative universe
   metadata as optional (never mandatory) terms;
5. treats queries with zero remaining content terms as low-information and
   uses a temporal-window candidate policy rather than an unbounded
   full-history generic event-term OR;
6. parses a target session date from the query when present so the retriever
   can apply deterministic temporal windows and ranking.

The policy is deterministic, contains no golden cause/answer/case/chunk
knowledge, and applies identically to the FTS5 and SQL fallback arms.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from catalyst_data.manifests.universe import RATIFIED_TICKERS

# English stopwords that carry no retrieval signal in attribution questions.
STOPWORDS = frozenset(
    """
    a about after all also am an and any are as at be because been before being
    but by can could did do does for from had has have how i if in into is it
    its just me more most my no not now of on or our out so some such than that
    the their them then there these they this those to too up us was we were
    what when where which who why will with would you your
    """.split()
)

MONTH_NAMES = frozenset(
    """
    january february march april may june july august september october
    november december jan feb mar apr jun jul aug sep sept oct nov dec
    """.split()
)

# Corporate-form tokens stripped when deriving issuer aliases from legal names.
_CORP_FORM_TOKENS = frozenset(
    """
    a an the and or of for in on to with by inc ltd limited corporation company
    co corp holdings holding group plc sa nv llc lp incorporated n v com
    """.split()
)

# Versioned universe metadata path (authoritative issuer names).
UNIVERSE_SPEC_PATH = (
    Path(__file__).resolve().parent.parent / "manifests" / "universe_v1_2025_08.spec.json"
)
UNIVERSE_METADATA_VERSION = "universe_v1_2025_08"

# Stable event vocabulary reserved for diagnostics only — never used as an
# unrestricted full-history OR clause (AMEND-5.1).
EVENT_QUERY_TERMS = (
    "move", "stock", "shares", "earnings", "results", "report", "filing",
    "quarter", "revenue",
)

# Zero content terms after structured stripping → temporal-window policy.
LOW_INFORMATION_MAX_CONTENT_TERMS = 0

# Deterministic expanding windows (calendar days on available_at date vs target).
# Rationale: attribution evidence for a session typically publishes same-day or
# within one trading session; 2 calendar days covers weekend/holiday adjacency
# without collapsing into full-history recall. 7 / 14 / 45 expand deterministically
# when the tight window is empty.
TEMPORAL_WINDOW_DAYS: tuple[int, ...] = (2, 7, 14, 45)


@dataclass(frozen=True)
class LexicalQueryPlan:
    raw_terms: tuple[str, ...]
    content_terms: tuple[str, ...]
    terms: tuple[str, ...]
    policy: str  # "content" | "temporal_window" | "event_fallback"
    low_information: bool
    target_date: str | None  # YYYY-MM-DD when parseable from the query
    structured_ticker: str
    issuer_enrichment: tuple[str, ...]


def _tokenize_legal_name(legal_name: str) -> tuple[str, ...]:
    tokens = re.findall(r"[a-z0-9]+", legal_name.casefold())
    return tuple(
        token for token in tokens
        if token not in _CORP_FORM_TOKENS and token not in STOPWORDS and not token.isdigit()
        and len(token) > 1
    )


@lru_cache(maxsize=1)
def _load_universe_issuer_maps() -> tuple[frozenset[str], dict[str, tuple[str, ...]], dict[str, str]]:
    """Load RATIFIED_TICKERS + legal_name-derived issuer maps from versioned metadata.

    Returns:
        known_tickers: uppercase ticker set (ratified universe)
        issuer_names: ticker -> enrichment tokens
        ticker_aliases: uppercase alias token -> ticker
    """
    known = frozenset(RATIFIED_TICKERS)
    issuer_names: dict[str, tuple[str, ...]] = {}
    alias_counts: dict[str, list[str]] = {}
    if UNIVERSE_SPEC_PATH.is_file():
        payload = json.loads(UNIVERSE_SPEC_PATH.read_text(encoding="utf-8"))
        companies = payload.get("companies") or {}
        for ticker in RATIFIED_TICKERS:
            company = companies.get(ticker) or {}
            legal_name = str(company.get("legal_name") or "")
            tokens = list(_tokenize_legal_name(legal_name))
            ticker_l = ticker.casefold()
            if ticker_l not in tokens:
                tokens.append(ticker_l)
            # Preserve order, drop duplicates.
            issuer_names[ticker] = tuple(dict.fromkeys(tokens))
            for token in issuer_names[ticker]:
                alias_counts.setdefault(token.upper(), []).append(ticker)
    else:
        for ticker in RATIFIED_TICKERS:
            issuer_names[ticker] = (ticker.casefold(),)

    # Unique tokens map to a single ticker; non-unique tokens map only when they
    # are the first enrichment token of exactly one issuer (brand headword).
    ticker_aliases: dict[str, str] = {}
    for alias, tickers in alias_counts.items():
        unique = list(dict.fromkeys(tickers))
        if len(unique) == 1:
            ticker_aliases[alias] = unique[0]
        else:
            heads = [
                ticker for ticker in unique
                if issuer_names.get(ticker) and issuer_names[ticker][0].upper() == alias
            ]
            if len(heads) == 1:
                ticker_aliases[alias] = heads[0]
    # Always accept the ticker symbol itself as an alias of itself.
    for ticker in RATIFIED_TICKERS:
        ticker_aliases[ticker] = ticker
    return known, issuer_names, ticker_aliases


def known_tickers() -> frozenset[str]:
    return _load_universe_issuer_maps()[0]


def issuer_name_map() -> dict[str, tuple[str, ...]]:
    return dict(_load_universe_issuer_maps()[1])


def ticker_alias_map() -> dict[str, str]:
    return dict(_load_universe_issuer_maps()[2])


# Eager module-level snapshots rebuilt from versioned universe metadata.
KNOWN_TICKERS: frozenset[str] = known_tickers()
ISSUER_NAMES: dict[str, tuple[str, ...]] = issuer_name_map()
TICKER_ALIASES: dict[str, str] = ticker_alias_map()

# ---------------------------------------------------------------------------
# AMEND-5.2A: conservative issuer *claims* for query identity extraction.
# Distinct from TICKER_ALIASES (used only to strip structured terms from FTS).
# Every unique legal-name token is NOT a reliable free-text issuer claim.
# ---------------------------------------------------------------------------

# Multi-word brand phrases (matched case-insensitively on the full query).
CONSERVATIVE_ISSUER_PHRASES: tuple[tuple[str, str], ...] = (
    ("taiwan semiconductor", "TSM"),
    ("jpmorgan chase", "JPM"),
    ("meta platforms", "META"),
    ("advanced micro devices", "AMD"),
)

# ---------------------------------------------------------------------------
# AMEND-5.2C: provenance-aware ticker/issuer *claims* for attribution identity.
#
# A claim is a typed span in the free-text query.  Only unambiguous foreign
# ticker *symbols* (explicit multi-char ALL-CAPS, or explicitly marked like
# $TSLA / NASDAQ:TSLA / NYSE:F) may hard-mismatch the structured target.
# Issuer brands/phrases and bare single letters are always ambiguous (they are
# normal peer/supplier/customer/news-subject mentions), so they fail open.
# This is a deliberate safety boundary: no word-blacklist patching is used to
# special-case individual English collisions.
# ---------------------------------------------------------------------------

# Bounded marked-symbol forms: $TSLA, NASDAQ:TSLA, NYSE:F (exchange prefixes
# are a fixed, reviewed set — no unbounded regex or external dependency).
_MARKED_SYMBOL_RE = re.compile(r"(?:\$|(?:NASDAQ|NYSE):)([A-Z]{1,8})\b")

# Finance English around single-letter tokens that are not ticker claims.
# Kept intentionally small: bare single letters are *always* ambiguous anyway,
# so this only improves claim precision for Series C / F grade / C-suite.
_SINGLE_LETTER_PREV_BLOCK = frozenset({
    "SERIES", "TYPE", "CLASS", "GRADE", "LEVEL", "RATING", "TIER", "PHASE",
})
_SINGLE_LETTER_NEXT_BLOCK = frozenset({
    "SUITE", "GRADE", "ROUND", "CLASS", "FUND", "SHARE", "SHARES", "LEVEL",
    "RATING", "TIER",
})


@dataclass(frozen=True)
class QueryTickerClaim:
    """One typed ticker/issuer claim with its exact provenance span.

    ``source`` is one of:
    - explicit_symbol: multi-char ALL-CAPS universe symbol (e.g. TSLA)
    - marked_symbol: $TSLA / NASDAQ:TSLA / NYSE:F (explicit by marking)
    - single_letter_symbol: bare ALL-CAPS C/F — always ambiguous
    - issuer_brand: Apple, Intel, Ford (conservative brand tokens)
    - issuer_phrase: Taiwan Semiconductor, Meta Platforms (full phrases)
    """

    ticker: str
    source: str
    raw_text: str
    start: int
    end: int


def _claim_span_overlaps(
    start: int,
    end: int,
    spans: list[tuple[int, int]],
) -> bool:
    return any(start < span_end and span_start < end for span_start, span_end in spans)


def collect_query_claims(
    query_text: str | None,
    known_tickers: set[str] | frozenset[str] | None = None,
) -> tuple[QueryTickerClaim, ...]:
    """Collect every typed ticker/issuer claim in free text.

    Returns ordered ``QueryTickerClaim`` records with exact provenance
    (source, raw_text, start, end).  Empty tuple means fail-open.  The caller's
    ``known_tickers`` restricts which symbols/brands are recognized; when
    omitted, the ratified universe is used.  Marked-symbol and issuer-phrase
    spans are consumed first so the same text is never double-claimed.
    """
    if not query_text:
        return ()
    universe = _load_universe_issuer_maps()[0]
    allowed = set(known_tickers) if known_tickers is not None else set(universe)
    phrases = CONSERVATIVE_ISSUER_PHRASES
    brands = CONSERVATIVE_ISSUER_BRANDS
    claims: list[QueryTickerClaim] = []
    reserved: list[tuple[int, int]] = []

    lowered = query_text.casefold()

    for match in _MARKED_SYMBOL_RE.finditer(query_text):
        symbol = match.group(1)
        if symbol in allowed:
            claims.append(QueryTickerClaim(
                ticker=symbol,
                source="marked_symbol",
                raw_text=match.group(0),
                start=match.start(),
                end=match.end(),
            ))
            reserved.append((match.start(), match.end()))

    for phrase, ticker in phrases:
        if ticker not in allowed:
            continue
        start = 0
        while True:
            index = lowered.find(phrase, start)
            if index == -1:
                break
            end = index + len(phrase)
            if not _claim_span_overlaps(index, end, reserved):
                claims.append(QueryTickerClaim(
                    ticker=ticker,
                    source="issuer_phrase",
                    raw_text=query_text[index:end],
                    start=index,
                    end=end,
                ))
                reserved.append((index, end))
            start = end

    matches = list(re.finditer(r"\b[A-Za-z]{1,12}\b", query_text))
    raw_tokens = [m.group(0) for m in matches]
    upper_tokens = [t.upper() for t in raw_tokens]
    for index, (match, raw, token) in enumerate(zip(matches, raw_tokens, upper_tokens, strict=True)):
        if _claim_span_overlaps(match.start(), match.end(), reserved):
            continue
        if len(token) >= 2 and token in allowed and raw.isupper():
            claims.append(QueryTickerClaim(
                ticker=token,
                source="explicit_symbol",
                raw_text=raw,
                start=match.start(),
                end=match.end(),
            ))
            continue
        if len(token) == 1 and token in allowed and raw.isupper():
            prev_tok = upper_tokens[index - 1] if index > 0 else None
            next_tok = upper_tokens[index + 1] if index + 1 < len(upper_tokens) else None
            if prev_tok in _SINGLE_LETTER_PREV_BLOCK:
                continue
            if next_tok in _SINGLE_LETTER_NEXT_BLOCK:
                continue
            claims.append(QueryTickerClaim(
                ticker=token,
                source="single_letter_symbol",
                raw_text=raw,
                start=match.start(),
                end=match.end(),
            ))
            continue
        mapped = brands.get(token)
        if mapped and mapped in allowed:
            claims.append(QueryTickerClaim(
                ticker=mapped,
                source="issuer_brand",
                raw_text=raw,
                start=match.start(),
                end=match.end(),
            ))

    return tuple(claims)


def claim_tickers(claims: tuple[QueryTickerClaim, ...] | list[QueryTickerClaim]) -> frozenset[str]:
    """Deduplicated ticker set for a claim collection (provenance preserved on
    the original records)."""
    return frozenset(claim.ticker for claim in claims)


def is_strong_symbol_claim(claim: QueryTickerClaim) -> bool:
    """A symbol claim explicit enough to identify a foreign direct target.

    Only marked symbols ($TSLA / NASDAQ:TSLA / NYSE:F) and explicit multi-char
    ALL-CAPS symbols qualify; bare single letters and issuer brands/phrases are
    always ambiguous and can never hard-mismatch the structured target.
    """
    return claim.source == "marked_symbol" or (
        claim.source == "explicit_symbol" and len(claim.ticker) >= 2
    )


# ---------------------------------------------------------------------------
# AMEND-5.2C follow-up: direct-target intent policy (small, deterministic,
# high-precision, fail-open).
#
# "A symbol is explicit" does not mean "that symbol is the analysis target".
# A foreign ticker may be a competitor, supplier, customer, news subject, or
# propagation context.  A hard mismatch is allowed only when the free-text
# query directly and unambiguously makes the foreign strong symbol the subject
# of a price-movement question, and the structured target is not that subject.
# Everything uncertain (comparisons, competitors, supply chains, customers,
# impact relationships) fails open.
# ---------------------------------------------------------------------------

_MOVEMENT_VERB_PATTERN = (
    r"(?:move[sd]?|moving|falls?|fell|fallen|falling|drop[sd]?|dropped|dropping|"
    r"decline[sd]?|declining|rise[s]?|rose|risen|rising|gain[sd]?|gained|gaining|"
    r"slip[s]?|slipped|slipping|surge[sd]?|surging|tumble[sd]?|tumbled|tumbling|"
    r"plunge[sd]?|plunged|plunging|rally|rallies|rallied|rallying|jump[s]?|jumped|jumping|"
    r"advance[sd]?|advanced|advancing|retreat[sd]?|retreated|retreating|"
    r"recover[s]?|recovered|recovering|rebound[s]?|rebounded|rebounding|"
    r"pop[s]?|popped|popping|selloff|sell-off)"
)

_MOVE_QUESTION_PREFIX = (
    r"(?:why did|why does|why is|why was|why has|"
    r"what caused|what drove|what led to|what triggered)"
)

_MARKET_DIRECTION = r"(?:down|up|higher|lower|flat)"

_IMPACT_VERB_PATTERN = (
    r"(?:hurt[s]?|hit[s]?|impact[s]?|impacted|affect[s]?|affected|damage[sd]?|"
    r"weigh(?:s|ed)?\s+on|drag(?:s|ged)?|pressur(?:e|es|ed|izes|ized)|hammer(?:s|ed)?)"
)


def _is_direct_move_subject(lowered_query: str, raw_pattern: str) -> bool:
    """True when the claim's symbol is the direct subject of a price-move
    question (interrogative, possessive, market-action, or declarative+why)."""
    # why did/what caused <SYM> (to )? <movement-verb>
    if re.search(
        rf"{_MOVE_QUESTION_PREFIX}\s+(?:the\s+)?{raw_pattern}\s+(?:to\s+)?{_MOVEMENT_VERB_PATTERN}\b",
        lowered_query,
    ):
        return True
    # why was/is <SYM> down/up/higher/lower
    if re.search(
        rf"(?:why (?:was|is)|what made)\s+(?:the\s+)?{raw_pattern}\s+{_MARKET_DIRECTION}\b",
        lowered_query,
    ):
        return True
    # why did <SYM> open/close/trade higher/lower
    if re.search(
        rf"(?:why did|why does)\s+(?:the\s+)?{raw_pattern}\s+(?:open|close|trade)\s+(?:sharply\s+)?{_MARKET_DIRECTION}\b",
        lowered_query,
    ):
        return True
    # what drove <SYM>'s move / <SYM>'s decline
    if re.search(
        rf"(?:what caused|what drove|what led to|what triggered|why)\s+(?:the\s+)?{raw_pattern}'s\s+{_MOVEMENT_VERB_PATTERN}\b",
        lowered_query,
    ):
        return True
    # declarative: <SYM> moved/fell ... (why|what caused|...) later
    match = re.search(rf"\b{raw_pattern}\s+{_MOVEMENT_VERB_PATTERN}\b", lowered_query)
    if match is not None and re.search(
        rf"\b(?:why|what caused|what drove|what led to|what triggered)\b",
        lowered_query[match.end():],
    ):
        return True
    return False


def identify_direct_target_claims(
    query: str | None,
    claims: tuple[QueryTickerClaim, ...] | list[QueryTickerClaim],
) -> tuple[QueryTickerClaim, ...]:
    """Claims whose symbol is the direct subject of a price-move question.

    Fail-open by construction: any frame that is not matched leaves the claim
    as a context mention (competitor/supplier/customer/news context), which
    can never hard-mismatch the structured target.
    """
    if not query or not claims:
        return ()
    lowered_query = query.casefold()
    return tuple(
        claim
        for claim in claims
        if _is_direct_move_subject(lowered_query, re.escape(claim.raw_text.casefold()))
    )


def identify_impact_target_claims(
    query: str | None,
    claims: tuple[QueryTickerClaim, ...] | list[QueryTickerClaim],
) -> tuple[QueryTickerClaim, ...]:
    """Claims that are the impacted object of an impact/attribution verb.

    Impact relationships always fail open for *foreign* symbols (the task
    policy: "影响关系都 fail-open"); this frame only confirms the structured
    ticker as the analysis target when it is explicitly the impacted entity.
    """
    if not query or not claims:
        return ()
    lowered_query = query.casefold()
    return tuple(
        claim
        for claim in claims
        if re.search(
            rf"\b{_IMPACT_VERB_PATTERN}\s+(?:the\s+)?{re.escape(claim.raw_text.casefold())}\b",
            lowered_query,
        )
    )


def _consistency_decision(
    claims: tuple[QueryTickerClaim, ...] | list[QueryTickerClaim],
    resolved: str,
    query: str,
) -> tuple[bool | None, str | None]:
    """Single source of truth for the direct-target consistency decision.

    Returns ``(decision, raw_symbol)``:
    - structured ticker is the direct price-move subject -> (True, resolved)
    - structured ticker is explicitly the impacted entity -> (True, resolved)
    - exactly one foreign strong symbol is the direct price-move subject and
      the structured ticker is not -> (False, that symbol)
    - everything else -> (None, None)
    """
    if not claims:
        return None, None
    direct = {claim.ticker for claim in identify_direct_target_claims(query, claims)}
    if resolved in direct:
        return True, resolved
    impacted = {claim.ticker for claim in identify_impact_target_claims(query, claims)}
    if resolved in impacted:
        return True, resolved
    foreign_direct = {
        claim.ticker
        for claim in claims
        if claim.ticker != resolved
        and is_strong_symbol_claim(claim)
        and claim.ticker in direct
    }
    if len(foreign_direct) == 1:
        return False, next(iter(foreign_direct))
    return None, None


def decide_claim_consistency(
    claims: tuple[QueryTickerClaim, ...] | list[QueryTickerClaim],
    resolved_ticker: str,
    *,
    query: str,
) -> bool | None:
    """Provenance-aware, direct-target-aware consistency decision.

    1. structured ticker is the direct price-move subject or impacted entity
       -> True
    2. exactly one foreign strong symbol is the direct price-move subject and
       the structured ticker is not -> False
    3. everything else (context mentions, comparisons, impact relationships,
       brands, bare single letters, multiple foreign symbols) -> None
    """
    decision, _symbol = _consistency_decision(
        claims, (resolved_ticker or "").upper(), query or "",
    )
    return decision


def direct_target_mismatch_symbol(
    claims: tuple[QueryTickerClaim, ...] | list[QueryTickerClaim],
    resolved_ticker: str,
    *,
    query: str,
) -> str | None:
    """The confirmed foreign direct-target symbol when the decision is False,
    otherwise None.  Used by the miner to stamp query_ticker_raw with the one
    symbol the user actually asked about."""
    _decision, symbol = _consistency_decision(
        claims, (resolved_ticker or "").upper(), query or "",
    )
    return symbol


# Distinctive single-token brands only — never generic legal-name fragments
# like "advanced", "platforms", "general", "target", "technology", "bank".
CONSERVATIVE_ISSUER_BRANDS: dict[str, str] = {
    "APPLE": "AAPL",
    "MICROSOFT": "MSFT",
    "TESLA": "TSLA",
    "GOOGLE": "GOOGL",
    "ALPHABET": "GOOGL",
    "NVIDIA": "NVDA",
    "AMAZON": "AMZN",
    "INTEL": "INTC",
    "ADOBE": "ADBE",
    "FORD": "F",
    "CITIGROUP": "C",
    "CITI": "C",
    # AMEND-5.2B: lone "Taiwan" is not a TSM claim; only "Taiwan Semiconductor"
    # (phrase) and the symbol TSM count.
    "JPMORGAN": "JPM",
    "UNITEDHEALTH": "UNH",
    "QUALCOMM": "QCOM",
    "NETFLIX": "NFLX",
    "COSTCO": "COST",  # brand, not lowercase "cost"
}


def conservative_issuer_brands() -> dict[str, str]:
    """Brand-token → ticker map for free-text issuer claims (not FTS stripping)."""
    return dict(CONSERVATIVE_ISSUER_BRANDS)


def conservative_issuer_phrases() -> tuple[tuple[str, str], ...]:
    return CONSERVATIVE_ISSUER_PHRASES


def normalize_query_terms(query: str) -> tuple[str, ...]:
    """NFC + casefold + word tokens, order preserved, duplicates removed."""
    normalized = unicodedata.normalize("NFC", query).casefold()
    return tuple(dict.fromkeys(re.findall(r"[^\W_]+", normalized, re.UNICODE)))


def _is_number(token: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", token))


def _is_ticker_token(token: str) -> bool:
    upper = token.upper()
    return upper in KNOWN_TICKERS or upper in TICKER_ALIASES


def _mentions_ticker(raw_terms: tuple[str, ...]) -> bool:
    """True when the raw query names an issuer/ticker (attribution signal)."""
    return any(_is_ticker_token(token) for token in raw_terms)


def _is_structured_token(token: str, ticker: str) -> bool:
    """Ticker / month / pure-number / structured-ticker tokens are never mandatory."""
    if token in MONTH_NAMES:
        return True
    if _is_number(token):
        return True
    if _is_ticker_token(token):
        return True
    # Structured ticker argument is always recognized and removed.
    if ticker and token.casefold() == ticker.casefold():
        return True
    # Issuer enrichment tokens for the structured ticker are structured too.
    for name in issuer_enrichment(ticker):
        if token.casefold() == name.casefold():
            return True
    return False


def issuer_enrichment(ticker: str) -> tuple[str, ...]:
    if not ticker:
        return ()
    return ISSUER_NAMES.get(ticker.upper(), (ticker.casefold(),))


_ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_ENGLISH_DATE_RE = re.compile(
    r"\b("
    r"january|february|march|april|may|june|july|august|september|october|"
    r"november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
    r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(20\d{2})\b",
    re.IGNORECASE,
)
_MONTH_NUM = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


def parse_target_date(query: str) -> str | None:
    """Extract a target session date (YYYY-MM-DD) from a free-text query.

    Prefers ISO dates; falls back to English month-day-year.  No case/oracle
    knowledge — purely structural parsing of the query text.

    AMEND-5.2: this value is diagnostic only.  Retrieval temporal center always
    comes from structured session/trade/cutoff via ``resolve_temporal_center``.
    """
    if not query:
        return None
    iso = _ISO_DATE_RE.search(query)
    if iso:
        year, month, day = iso.group(1), iso.group(2), iso.group(3)
        return f"{year}-{month}-{day}"
    eng = _ENGLISH_DATE_RE.search(query)
    if eng:
        month = _MONTH_NUM[eng.group(1).lower()]
        day = int(eng.group(2))
        year = int(eng.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"
    return None


@dataclass(frozen=True)
class TemporalCenterResolution:
    """Structured temporal center for lexical retrieval ranking/windows.

    ``center_date`` is always derived from structured session identity
    (session_date / trade_date / cutoff date).  A free-text query date is
    recorded for conflict detection but never overrides the center.
    """

    center_date: str | None  # YYYY-MM-DD
    query_date: str | None  # YYYY-MM-DD when parseable from free text
    conflict: bool
    decision: str  # "structured" | "structured_ignore_query" | "none"


def _iso_day(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if len(text) >= 10:
        day = text[:10]
        if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", day):
            return day
    return None


def resolve_temporal_center(
    *,
    query: str | None = None,
    cutoff: str | None = None,
    session_date: str | None = None,
    trade_date: str | None = None,
) -> TemporalCenterResolution:
    """Resolve the retrieval temporal center from structured session fields.

    Priority for the center: ``session_date`` > ``trade_date`` > cutoff day.
    Free-text query dates never override.  When the query claims a different
    day, the conflict is recorded and the structured center is kept
    (``decision=structured_ignore_query``).
    """
    center = (
        _iso_day(session_date)
        or _iso_day(trade_date)
        or _iso_day(cutoff)
    )
    query_date = parse_target_date(query or "")
    if center is None:
        return TemporalCenterResolution(
            center_date=None,
            query_date=query_date,
            conflict=False,
            decision="none",
        )
    if query_date is None or query_date == center:
        return TemporalCenterResolution(
            center_date=center,
            query_date=query_date,
            conflict=False,
            decision="structured",
        )
    return TemporalCenterResolution(
        center_date=center,
        query_date=query_date,
        conflict=True,
        decision="structured_ignore_query",
    )


def plan_lexical_query(query: str, ticker: str) -> LexicalQueryPlan:
    raw_terms = normalize_query_terms(query)
    structured = (ticker or "").upper()
    content = tuple(
        token for token in raw_terms
        if token not in STOPWORDS and not _is_structured_token(token, structured)
    )
    low_information = len(content) <= LOW_INFORMATION_MAX_CONTENT_TERMS
    attribution_query = _mentions_ticker(raw_terms) or bool(structured)
    enrichment = issuer_enrichment(structured) if structured else ()
    target_date = parse_target_date(query)
    terms: list[str] = []
    if low_information and attribution_query:
        # AMEND-5.1: do not expand into unrestricted event-term OR.
        # Temporal-window retrieval ranks eligible chunks by lag to target.
        policy = "temporal_window"
        terms.extend(token for token in content)
    else:
        policy = "content"
        terms.extend(token for token in content)
    if attribution_query and not low_information:
        for name in enrichment:
            if name not in terms:
                terms.append(name)
    return LexicalQueryPlan(
        raw_terms=raw_terms,
        content_terms=content,
        terms=tuple(terms),
        policy=policy,
        low_information=low_information,
        target_date=target_date,
        structured_ticker=structured,
        issuer_enrichment=enrichment,
    )


def content_terms(query: str, ticker: str) -> tuple[str, ...]:
    """Public helper: the final optional term set for a query (tests/audit)."""
    return plan_lexical_query(query, ticker).terms


def fts_quote(term: str) -> str:
    """Quote one term for an FTS5 MATCH expression with defensive escaping.

    Terms are produced by the word-token regex; anything else (embedded
    quotes, operators, whitespace) is rejected rather than risked in MATCH.
    """
    if re.fullmatch(r"[^\W_]+", term, re.UNICODE) is None:
        raise ValueError(f"unsafe FTS5 term: {term!r}")
    return f'"{term}"'


def compile_match(terms: tuple[str, ...], *, mode: str) -> str:
    if mode not in {"and", "or"}:
        raise ValueError("compile_match mode must be and or or")
    if not terms:
        return ""
    quoted = [fts_quote(term) for term in terms]
    return f" {mode.upper()} ".join(quoted)


def calendar_day_lag(available_at: str, target_date: str) -> float:
    """Absolute calendar-day lag between available_at and target YYYY-MM-DD."""
    from datetime import date, datetime

    target = date.fromisoformat(target_date)
    day = available_at[:10]
    try:
        avail = date.fromisoformat(day)
    except ValueError:
        try:
            avail = datetime.strptime(available_at, "%Y-%m-%dT%H:%M:%SZ").date()
        except ValueError:
            return 1e9
    return float(abs((avail - target).days))


__all__ = [
    "CONSERVATIVE_ISSUER_BRANDS", "CONSERVATIVE_ISSUER_PHRASES",
    "EVENT_QUERY_TERMS", "ISSUER_NAMES", "KNOWN_TICKERS",
    "LOW_INFORMATION_MAX_CONTENT_TERMS", "LexicalQueryPlan", "QueryTickerClaim",
    "STOPWORDS", "TEMPORAL_WINDOW_DAYS", "TICKER_ALIASES",
    "TemporalCenterResolution", "UNIVERSE_METADATA_VERSION", "UNIVERSE_SPEC_PATH",
    "calendar_day_lag", "claim_tickers", "collect_query_claims", "compile_match",
    "conservative_issuer_brands", "conservative_issuer_phrases", "content_terms",
    "decide_claim_consistency", "direct_target_mismatch_symbol", "fts_quote",
    "identify_direct_target_claims", "identify_impact_target_claims",
    "is_strong_symbol_claim", "issuer_enrichment", "issuer_name_map",
    "known_tickers", "normalize_query_terms", "parse_target_date",
    "plan_lexical_query", "resolve_temporal_center", "ticker_alias_map",
    "_mentions_ticker",
]
