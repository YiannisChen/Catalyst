"""Per-evidence event extraction and deterministic publication decisions."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

EXTRACTION_PROMPT_VERSION = "m1_extract_v2"


class _StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class AboutTicker(_StrEnum):
    ISSUER = "ISSUER"
    GROUP = "GROUP"
    OTHER = "OTHER"


class Session(_StrEnum):
    THIS_SESSION = "THIS_SESSION"
    OTHER_SESSION = "OTHER_SESSION"
    UNCLEAR = "UNCLEAR"


class EventType(_StrEnum):
    EARNINGS = "EARNINGS"
    GUIDANCE = "GUIDANCE"
    ANALYST_ACTION = "ANALYST_ACTION"
    DEAL_MA = "DEAL_MA"
    REGULATORY_LEGAL = "REGULATORY_LEGAL"
    PRODUCT_OPERATIONS = "PRODUCT_OPERATIONS"
    MANAGEMENT = "MANAGEMENT"
    CAPITAL_RETURN = "CAPITAL_RETURN"
    MACRO = "MACRO"
    SECTOR = "SECTOR"
    COMMODITY = "COMMODITY"
    OTHER = "OTHER"
    NONE = "NONE"


class EventTiming(_StrEnum):
    BEFORE_OPEN = "BEFORE_OPEN"
    INTRADAY = "INTRADAY"
    PRIOR_DAY = "PRIOR_DAY"
    AFTER_CLOSE = "AFTER_CLOSE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Extraction:
    about_ticker: AboutTicker | None = None
    session: Session | None = None
    event_type: EventType | None = None
    event_summary: str = ""
    event_timing: EventTiming | None = None
    explicit_attribution: bool = False
    attribution_quote: str = ""
    event_quote: str = ""
    mechanism_quote: str = ""
    is_preview_or_evergreen: bool = False
    valid: bool = True
    invalid_reason: str | None = None
    quote_failures: tuple[str, ...] = ()
    attribution_roles: tuple[str, ...] | None = None
    event_roles: tuple[str, ...] | None = None

    @classmethod
    def invalid(cls, reason: str) -> "Extraction":
        return cls(valid=False, invalid_reason=reason)


def build_extraction_messages(facts: dict, item: dict) -> list[dict]:
    """Build a two-message, single-item extraction request."""
    observation = facts.get("observation", facts)
    session_date = str(facts.get("session_date") or "unknown session")
    try:
        day = datetime.fromisoformat(session_date).strftime("%A, %Y-%m-%d")
    except ValueError:
        day = session_date
    def value(obj: Any, key: str, default: Any = None) -> Any:
        return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)
    def move(key: str) -> str:
        v = value(observation, key)
        if not isinstance(v, dict) and not hasattr(v, "__dict__"):
            return "unavailable"
        ret = value(v, "reference_return_pct")
        relation = value(v, "direction_relation", "UNKNOWN")
        band = value(v, "band")
        return f"return={ret!s}; relation={relation}; band={band!s}"
    identity = facts.get("temporal_identity")
    if not session_date or session_date == "unknown session":
        session_date = str(value(identity, "session_date", "unknown session"))
        day = session_date
    ticker = facts.get("ticker") or value(observation, "ticker", "unknown")
    company = facts.get("company_name") or facts.get("company") or "unknown"
    excerpt = str(item.get("excerpt_text") or "")[:2500]
    content = {
        "ticker": ticker, "company_name": company, "session_date": day,
        "target_return": facts.get("target_return", value(observation, "target_return")),
        "market_return": facts.get("market_return", move("market_comove")),
        "sector_return": facts.get("sector_return", move("sector_comove")),
        "market_comove": move("market_comove"), "sector_comove": move("sector_comove"),
    }
    user = (
        "Extract only what this one evidence item says. Read only its excerpt; use no outside knowledge. "
        "Copy complete source sentences exactly, including punctuation. A later recap explaining the price move "
        f"on {day} is THIS_SESSION. Facts: {json.dumps(content, ensure_ascii=False)}\n"
        f"Publisher: {item.get('publisher') or 'unknown'}\n"
        f"Published time (eligible_at): {item.get('eligible_at') or 'unknown'}\n"
        f"Excerpt:\n{excerpt}\n\nReturn JSON only with these exact fields and enum values: "
        '{"about_ticker":"ISSUER|GROUP|OTHER","session":"THIS_SESSION|OTHER_SESSION|UNCLEAR",'
        '"event_type":"EARNINGS|GUIDANCE|ANALYST_ACTION|DEAL_MA|REGULATORY_LEGAL|PRODUCT_OPERATIONS|'
        'MANAGEMENT|CAPITAL_RETURN|MACRO|SECTOR|COMMODITY|OTHER|NONE","event_summary":"at most 20 words or empty",'
        '"event_timing":"BEFORE_OPEN|INTRADAY|PRIOR_DAY|AFTER_CLOSE|UNKNOWN",'
        '"explicit_attribution":false,"attribution_quote":"exact attribution sentence or empty",'
        '"event_quote":"exact event sentence or empty","mechanism_quote":"exact sentence or empty",'
        '"is_preview_or_evergreen":false}. '
        "GROUP means the market, sector, commodity, or peers this stock trades with. Explicit attribution is true "
        "only when this text itself causally links an event to this stock, or its group for GROUP, with language "
        "such as after, on, as, due to, following, amid, or sent shares. mechanism_quote is the one sentence "
        "that says why the event mattered to investors: how it compared with expectations or guidance, the "
        "specific metric or detail that disappointed or impressed, or what investors focused on. Leave it "
        "empty if the text gives no such reason; never use the attribution or event sentence again."
    )
    return [
        {"role": "system", "content": "You extract events from a single financial evidence excerpt. Do not infer beyond the text."},
        {"role": "user", "content": user},
    ]


def parse_extraction(raw: str) -> Extraction:
    """Parse strict JSON; malformed or schema-invalid model output never raises."""
    try:
        text = str(raw).strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
        payload = json.loads(text)
        if not isinstance(payload, dict):
            return Extraction.invalid("JSON must be an object")
        required = {
            "about_ticker", "session", "event_type", "event_summary", "event_timing",
            "explicit_attribution", "attribution_quote", "event_quote", "mechanism_quote", "is_preview_or_evergreen",
        }
        if set(payload) != required:
            return Extraction.invalid("missing or unexpected fields")
        if type(payload["explicit_attribution"]) is not bool or type(payload["is_preview_or_evergreen"]) is not bool:
            return Extraction.invalid("boolean field has wrong type")
        if any(not isinstance(payload[k], str) for k in ("event_summary", "attribution_quote", "event_quote", "mechanism_quote")):
            return Extraction.invalid("text field has wrong type")
        if len(payload["event_summary"].split()) > 20:
            return Extraction.invalid("event_summary exceeds 20 words")
        return Extraction(
            about_ticker=AboutTicker(payload["about_ticker"]), session=Session(payload["session"]),
            event_type=EventType(payload["event_type"]), event_summary=payload["event_summary"],
            event_timing=EventTiming(payload["event_timing"]),
            explicit_attribution=payload["explicit_attribution"], attribution_quote=payload["attribution_quote"],
            event_quote=payload["event_quote"], mechanism_quote=payload["mechanism_quote"],
            is_preview_or_evergreen=payload["is_preview_or_evergreen"],
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return Extraction.invalid(f"invalid extraction: {type(exc).__name__}")


def validate_quotes(extraction: Extraction, item: dict, temporal_identity: Any, policy: str) -> Extraction:
    """Apply the existing quote rules separately to attribution and event sentences."""
    if not extraction.valid:
        return extraction
    from catalyst_agents.attribution.quotes import check_quote
    try:
        eligible_at = item.get("eligible_at")
        if isinstance(eligible_at, str):
            eligible_at = datetime.fromisoformat(eligible_at.replace("Z", "+00:00"))
        if not isinstance(eligible_at, datetime) or eligible_at.tzinfo is None:
            raise ValueError("eligible_at must be timezone-aware")
        evidence_id = str(item.get("evidence_id") or "")
        ns = SimpleNamespace(
            content_state=item.get("content_state"), eligible_at=eligible_at,
            excerpt_text=str(item.get("excerpt_text") or ""), source_class=item.get("source_class"),
            canonical_url=item.get("canonical_url"), publisher=item.get("publisher"),
            release_timing=item.get("_earnings_release_timing"),
        )
        kwargs = dict(supporting_ids={evidence_id}, items={evidence_id: ns}, temporal_identity=temporal_identity,
                      evidence_window_policy=policy)
        failures: list[str] = []
        attribution = extraction.attribution_quote
        event = extraction.event_quote
        if attribution and check_quote(attribution, evidence_id, **kwargs) != "OK":
            failures.append("attribution:" + check_quote(attribution, evidence_id, **kwargs))
            attribution = ""
        if event and check_quote(event, evidence_id, **kwargs) != "OK":
            failures.append("event:" + check_quote(event, evidence_id, **kwargs))
            event = ""
        mechanism = extraction.mechanism_quote
        if mechanism and check_quote(mechanism, evidence_id, **kwargs) != "OK":
            failures.append("mechanism:" + check_quote(mechanism, evidence_id, **kwargs))
            mechanism = ""
        checked = Extraction(**{
            **extraction.__dict__, "attribution_quote": attribution, "event_quote": event, "mechanism_quote": mechanism,
            "explicit_attribution": extraction.explicit_attribution and bool(attribution),
            "quote_failures": tuple(failures),
        })
        if checked.valid and item.get("_earnings_release_timing"):
            from catalyst_agents.runtime.earnings_timing import apply_earnings_timing
            checked = apply_earnings_timing(
                checked, item["_earnings_release_timing"],
                session_date=temporal_identity.session_date,
            )
        return checked
    except Exception as exc:  # malformed persisted timestamps/policy fail closed
        return Extraction(**{
            **extraction.__dict__, "attribution_quote": "", "event_quote": "", "mechanism_quote": "",
            "explicit_attribution": False,
            "quote_failures": extraction.quote_failures + (f"validation:{type(exc).__name__}",),
        })


def attach_quote_roles(
    extraction: Extraction,
    item: dict,
    temporal_identity: Any,
    policy: str,
    *,
    ticker: str,
    move_profile: Any | None = None,
) -> Extraction:
    """Attach the classifier's roles to validated quotes; role lookup never changes quote validity."""
    if not extraction.valid:
        return extraction
    try:
        from catalyst_agents.attribution.quotes import classify_extraction_quote_roles

        eligible_at = item.get("eligible_at")
        if isinstance(eligible_at, str):
            eligible_at = datetime.fromisoformat(eligible_at.replace("Z", "+00:00"))
        if not isinstance(eligible_at, datetime) or eligible_at.tzinfo is None:
            return extraction
        ns = SimpleNamespace(
            content_state=item.get("content_state"), eligible_at=eligible_at,
            excerpt_text=str(item.get("excerpt_text") or ""), source_class=item.get("source_class"),
            canonical_url=item.get("canonical_url"), publisher=item.get("publisher"),
        )
        attribution_roles = event_roles = None
        if extraction.attribution_quote:
            attribution_roles = classify_extraction_quote_roles(
                extraction.attribution_quote, ns, ticker=ticker, temporal_identity=temporal_identity,
                evidence_window_policy=policy, move_profile=move_profile,
            ).roles
        if extraction.event_quote:
            event_roles = classify_extraction_quote_roles(
                extraction.event_quote, ns, ticker=ticker, temporal_identity=temporal_identity,
                evidence_window_policy=policy, move_profile=move_profile,
            ).roles
        return Extraction(**{
            **extraction.__dict__, "attribution_roles": attribution_roles, "event_roles": event_roles,
        })
    except Exception:
        return extraction


_UP = re.compile(r"\b(?:rose|rises|rising|gain(?:s|ed|ing)?|jump(?:s|ed|ing)?|surg(?:e|es|ed|ing)|soar(?:s|ed|ing)?|"
                 r"rall(?:y|ies|ied|ying)|climb(?:s|ed|ing)?|advanc(?:e|es|ed|ing)|higher|up \d)", re.I)
_DOWN = re.compile(r"\b(?:fell|falls|falling|drop(?:s|ped|ping)?|slid(?:es|ing)?|slump(?:s|ed|ing)?|plung(?:e|es|ed|ing)|"
                   r"tumbl(?:e|es|ed|ing)|sank|sink(?:s|ing)?|declin(?:e|es|ed|ing)|lower|down \d|sell-off|selloff)", re.I)


def check_direction(extraction: Extraction, target_return: Any) -> Extraction:
    """Blank an attribution whose stated move contradicts the observed return (INTC 'gained' on a down day)."""
    try:
        sign = float(target_return)
    except (TypeError, ValueError):
        return extraction
    quote = extraction.attribution_quote
    if not quote or sign == 0:
        return extraction
    up, down = len(_UP.findall(quote)), len(_DOWN.findall(quote))
    stated = 1 if up > down else -1 if down > up else 0
    if stated == 0 or (stated > 0) == (sign > 0):
        return extraction
    return Extraction(**{**extraction.__dict__, "attribution_quote": "", "explicit_attribution": False,
                         "attribution_roles": None,
                         "quote_failures": extraction.quote_failures + ("attribution:DIRECTION_CONFLICT",)})


_STOPWORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "its", "of", "on", "or", "the", "to", "with", "after", "amid", "shares", "stock"}
_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(summary: str) -> set[str]:
    return {w for w in _WORD_RE.findall(summary.lower()) if w not in _STOPWORDS}


def _jaccard(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    return len(a & b) / len(a | b) if a or b else 0.0


def _origin(item: dict) -> str:
    for key in ("dedup_cluster_id", "cluster_id", "dedup_id", "dedup_group_id"):
        if item.get(key):
            return str(item[key])
    host = (urlsplit(str(item.get("canonical_url") or "")).hostname or "").lower().removeprefix("www.")
    if host.endswith("yahoo.com"):
        return "yahoo.com"
    if host:
        return host
    return str(item.get("publisher") or "unknown").strip().lower()


@dataclass(frozen=True)
class EventCluster:
    event_type: EventType
    members: tuple[tuple[dict, Extraction], ...]
    independent_origins: int
    explicit_origins: int
    has_primary_source: bool
    scope: AboutTicker
    best_attribution: tuple[dict, str] | None
    best_event: tuple[dict, str] | None
    this_session: bool = False
    timings: frozenset = frozenset()
    best_mechanism: tuple[dict, str] | None = None
    best_attribution_roles: tuple[str, ...] | None = None
    best_event_roles: tuple[str, ...] | None = None

    @property
    def earliest(self) -> datetime:
        return min((_eligible(m[0]) for m in self.members), default=datetime.max.replace(tzinfo=timezone.utc))


def _eligible(item: dict) -> datetime:
    value = item.get("eligible_at")
    if isinstance(value, datetime):
        return value
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)


def _make_cluster(records: list[tuple[dict, Extraction]]) -> EventCluster:
    origins = {_origin(item) for item, _ in records}
    explicit = {_origin(item) for item, ext in records if ext.explicit_attribution and ext.attribution_quote}
    primary = any(item.get("source_class") in {"issuer_disclosure", "corporate_press_release"} and ext.event_quote
                  for item, ext in records)
    scopes = [ext.about_ticker for _, ext in records]
    scope = AboutTicker.ISSUER if AboutTicker.ISSUER in scopes else AboutTicker.GROUP
    ordered = sorted(records, key=lambda pair: (not (pair[1].explicit_attribution and pair[1].attribution_quote),
                                                 not bool(pair[1].event_quote), not (pair[0].get("source_class") in {"issuer_disclosure", "corporate_press_release"}),
                                                 _eligible(pair[0]), str(pair[0].get("evidence_id", ""))))
    attr = next(((it, ex.attribution_quote) for it, ex in ordered if ex.explicit_attribution and ex.attribution_quote), None)
    event = next(((it, ex.event_quote) for it, ex in ordered if ex.event_quote), None)
    this_session = any(ext.session == Session.THIS_SESSION for _, ext in records)
    timings = frozenset(ext.event_timing for _, ext in records if ext.event_timing is not None)
    mechanism = next(((it, ex.mechanism_quote) for it, ex in ordered if ex.mechanism_quote), None)
    attribution_roles = next(
        (ex.attribution_roles for _, ex in ordered if ex.explicit_attribution and ex.attribution_quote), None)
    event_roles = next((ex.event_roles for _, ex in ordered if ex.event_quote), None)
    return EventCluster(records[0][1].event_type, tuple(ordered), len(origins), len(explicit), primary, scope, attr,
                        event, this_session, timings, mechanism, attribution_roles, event_roles)


def cluster_events(records: list[tuple[dict, Extraction]]) -> list[EventCluster]:
    """Group same-session event descriptions using deterministic token overlap."""
    usable = [(i, e) for i, e in records if e.valid and e.about_ticker != AboutTicker.OTHER
              and e.session != Session.OTHER_SESSION and e.event_type != EventType.NONE
              and not e.is_preview_or_evergreen and e.event_type is not None]
    parents = list(range(len(usable)))
    def find(i: int) -> int:
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    for a in range(len(usable)):
        for b in range(a + 1, len(usable)):
            ia, ea = usable[a]; ib, eb = usable[b]
            if ea.event_type != eb.event_type:
                continue
            cutoff = 0.2 if ea.event_type in {EventType.MACRO, EventType.SECTOR, EventType.COMMODITY} else 0.25
            related = _jaccard(ea.event_summary, eb.event_summary) >= cutoff
            same_issuer_earnings = (ea.event_type in {EventType.EARNINGS, EventType.GUIDANCE}
                                    and ea.about_ticker == eb.about_ticker == AboutTicker.ISSUER)
            if related or same_issuer_earnings:
                ra, rb = find(a), find(b)
                parents[max(ra, rb)] = min(ra, rb)
    groups: dict[int, list[tuple[dict, Extraction]]] = {}
    for index, record in enumerate(usable):
        groups.setdefault(find(index), []).append(record)
    clusters = [_make_cluster(group) for group in groups.values()]
    return sorted(clusters, key=lambda c: (c.event_type.value, c.earliest, tuple(sorted(_origin(i) for i, _ in c.members))))


@dataclass(frozen=True)
class GateConfig:
    variant: str = "strict"
    def __post_init__(self) -> None:
        if self.variant not in {"strict", "standard", "nogate", "desk", "desk_m3"}:
            raise ValueError("variant must be strict, standard, nogate, desk, or desk_m3")


@dataclass(frozen=True)
class GateDecision:
    publish: bool
    primary: EventCluster | None
    secondary: EventCluster | None
    reason_code: str
    diagnostics: tuple[dict, ...] = field(default_factory=tuple)
    # REPORTED_ATTRIBUTION: a source states the link. CONFIRMED_EVENT_INFERRED_LINK: the session's only
    # material company event is confirmed and the link is Catalyst's reading. GROUP_DRIVER: a source states
    # why the market/sector/commodity moved and the stock tracked that group (code facts).
    basis: str | None = None


def _same_direction(comove: Any) -> bool:
    if isinstance(comove, dict):
        return str(comove.get("direction_relation", "")).upper() == "SAME_DIRECTION"
    return str(getattr(comove, "direction_relation", "")).upper() == "SAME_DIRECTION"


_MATERIAL_ISSUER_EVENTS = {EventType.EARNINGS, EventType.GUIDANCE, EventType.DEAL_MA, EventType.REGULATORY_LEGAL,
                           EventType.MANAGEMENT, EventType.ANALYST_ACTION, EventType.CAPITAL_RETURN,
                           EventType.PRODUCT_OPERATIONS}
_GROUP_EVENTS = {EventType.MACRO, EventType.SECTOR, EventType.COMMODITY}
_PRE_MOVE_TIMINGS = {EventTiming.BEFORE_OPEN, EventTiming.INTRADAY, EventTiming.PRIOR_DAY}
# Which same-session group events can explain a move that the decomposition assigns to each reference.
_GROUP_EVENTS_FOR = {
    "MARKET": frozenset({EventType.MACRO}),
    "SECTOR": frozenset({EventType.SECTOR, EventType.MACRO}),
    "PEERS": frozenset({EventType.SECTOR, EventType.MACRO}),
    "COMMODITY": frozenset({EventType.COMMODITY, EventType.MACRO}),
}


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _group_explains_move(event_type: EventType, observation: Any) -> bool:
    """A group driver explains the move only when the stock tracked that group (band ALIGNED), or, for a
    commodity event, when the commodity moved the same way. Same direction alone is not enough: a stock
    that fell 4% on a 1% market day mostly moved for its own reasons (v6 dev: MMM, C)."""
    get = (lambda k: observation.get(k)) if isinstance(observation, dict) else (lambda k: getattr(observation, k, None))
    def band(key: str) -> str:
        value = get(key)
        raw = value.get("band") if isinstance(value, dict) else getattr(value, "band", None)
        return str(getattr(raw, "value", raw) or "").upper()
    if event_type == EventType.COMMODITY:
        try:
            return float(get("commodity_return")) * float(get("target_return")) > 0
        except (TypeError, ValueError):
            return False
    return any(band(k) == "ALIGNED" for k in ("market_comove", "sector_comove", "peer_comove"))


def _same_evidence(left: dict, right: dict) -> bool:
    left_id = str(left.get("evidence_id") or "")
    right_id = str(right.get("evidence_id") or "")
    if left_id or right_id:
        return bool(left_id) and left_id == right_id
    return _origin(left) == _origin(right)


def _lead_attribution_paired_with_same_source_event(cluster: EventCluster) -> bool:
    """The one pairing the owner's rule closes: LEAD attribution plus a same-item EVENT quote."""
    attribution = cluster.best_attribution
    event = cluster.best_event
    attribution_roles = cluster.best_attribution_roles
    event_roles = cluster.best_event_roles
    if attribution is None or event is None or not attribution_roles or not event_roles:
        return False
    if "LEAD" not in attribution_roles or "EVENT" not in event_roles:
        return False
    if " ".join(attribution[1].split()) == " ".join(event[1].split()):
        return False
    return _same_evidence(attribution[0], event[0])


def _classifier_quote_trace(pair: tuple[dict, str] | None, roles: tuple[str, ...] | None) -> dict | None:
    if pair is None:
        return None
    item, quote = pair
    return {
        "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        "evidence_id": item.get("evidence_id"),
        "roles": list(roles or ()),
    }


def _pairing_rule_outcome(cluster: EventCluster, *, enabled: bool) -> str:
    if not enabled:
        return "not_applicable"
    attribution_roles = cluster.best_attribution_roles or ()
    event_roles = cluster.best_event_roles or ()
    if (cluster.best_attribution is None or cluster.best_event is None
            or "LEAD" not in attribution_roles or "EVENT" not in event_roles):
        return "not_applicable"
    return ("rejected_lead_same_source_event"
            if _lead_attribution_paired_with_same_source_event(cluster) else "passed")


def select_and_gate(clusters: list[EventCluster], facts: dict, config: GateConfig) -> GateDecision:
    """Rank by the documented lexicographic score; earliest eligible source breaks ties.

    The desk variant is standard plus two inference paths a desk analyst uses when no source states the
    link: a confirmed material company event timed before or during the session, and a same-session
    group driver for a stock that moved with that group. A LEAD attribution quote cannot pass by pairing
    with a separate EVENT-role quote from the same evidence item. Reported attributions always rank first.
    """
    observation = facts.get("observation", facts)
    market = _same_direction(observation.get("market_comove") if isinstance(observation, dict) else getattr(observation, "market_comove", None))
    sector = _same_direction(observation.get("sector_comove") if isinstance(observation, dict) else getattr(observation, "sector_comove", None))
    idiosyncratic = not (market or sector)
    m3 = config.variant == "desk_m3"
    decomposition = facts.get("decomposition") if m3 else None
    verdict = getattr(decomposition, "verdict", "UNAVAILABLE")
    best_kind = getattr(getattr(decomposition, "best", None), "kind", None)
    if m3 and verdict != "UNAVAILABLE":
        # The beta decomposition replaces the size-only comovement band (M3).
        idiosyncratic = verdict != "GROUP_DRIVEN"
    eligible: list[tuple[EventCluster, str]] = []
    diagnostics = []
    issuer_events = sum(1 for c in clusters if c.scope == AboutTicker.ISSUER and c.this_session
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
            if (cluster.scope == AboutTicker.ISSUER and cluster.event_type in _MATERIAL_ISSUER_EVENTS and timed
                    and (cluster.has_primary_source or cluster.independent_origins >= 2)
                    and issuer_events == 1):
                threshold, basis = True, "CONFIRMED_EVENT_INFERRED_LINK"
        pairing_rule = _pairing_rule_outcome(cluster, enabled=config.variant in ("desk", "desk_m3"))
        blocked_pairing = pairing_rule == "rejected_lead_same_source_event"
        allowed = group_ok and has_event and threshold and not blocked_pairing
        diagnostics.append({"event_type": cluster.event_type.value, "scope": cluster.scope.value,
                            "independent_origins": cluster.independent_origins, "explicit_origins": cluster.explicit_origins,
                            "has_primary_source": cluster.has_primary_source, "has_event_quote": has_event,
                            "group_consistent": group_ok, "this_session": cluster.this_session,
                            "basis": basis if allowed else None, "eligible": allowed,
                            "best_attribution": _classifier_quote_trace(
                                cluster.best_attribution, cluster.best_attribution_roles),
                            "best_event": _classifier_quote_trace(cluster.best_event, cluster.best_event_roles),
                            "pairing_rule": pairing_rule})
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


def _quoted(item: dict, quote: str) -> str:
    publisher = str(item.get("publisher") or "Unknown publisher")
    try:
        timestamp = _eligible(item).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError):
        timestamp = "unknown time"
    return f"“{quote}” — {publisher}, {timestamp}"


MAX_RENDERED_QUOTES = 3
MAX_RENDERED_CHARS = 1_800  # quotes.MAX_TOTAL_QUOTE_CHARS


def render_statement(cluster: EventCluster, max_quotes: int = MAX_RENDERED_QUOTES) -> str:
    """Up to three verbatim sentences: the attribution, the event, then corroboration from other origins.

    v6 dev: a single quote was often judged PARTIAL where P's multi-quote answer was a MATCH, because
    the mechanism (why the event mattered) sat in a second sentence or a second article.
    """
    chosen: list[tuple[dict, str]] = []
    seen_quotes: set[str] = set()
    used_origins: set[str] = set()

    def add(pair: tuple[dict, str] | None) -> None:
        if pair is None or len(chosen) >= max_quotes:
            return
        item, quote = pair
        if not quote or quote in seen_quotes:
            return
        chosen.append(pair)
        seen_quotes.add(quote)
        used_origins.add(_origin(item))

    add(cluster.best_attribution)
    add(cluster.best_mechanism)  # why investors reacted (v6 dev: most PARTIALs lacked it)
    add(cluster.best_event)
    # Corroboration: other origins first, attribution sentences before event sentences.
    for prefer_attribution in (True, False):
        for item, ext in cluster.members:
            if ext is None or _origin(item) in used_origins:
                continue
            quote = ext.attribution_quote if prefer_attribution else ext.event_quote
            if prefer_attribution and not ext.explicit_attribution:
                continue
            add((item, quote))
    parts: list[str] = []
    total = 0
    for item, quote in chosen:
        rendered = _quoted(item, quote)
        if len(quote) > 900 or total + len(quote) > MAX_RENDERED_CHARS:
            continue  # quotes are dropped whole, never truncated
        parts.append(rendered)
        total += len(quote)
    return "\n".join(parts)


__all__ = ["AboutTicker", "EventCluster", "EventTiming", "EventType", "EXTRACTION_PROMPT_VERSION", "Extraction", "GateConfig", "GateDecision", "Session", "attach_quote_roles", "build_extraction_messages", "check_direction", "cluster_events", "parse_extraction", "render_statement", "select_and_gate", "validate_quotes"]
