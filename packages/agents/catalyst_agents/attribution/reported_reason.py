"""A bounded, verbatim source reason shown alongside an abstention."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from typing import Any, Callable

from catalyst_agents.attribution._format import _pct

REPORTED_REASON_ROLE = "reported_reason"
REPORTED_REASON_VERSION = "reported_reason_v4"
MAX_OUTPUT_TOKENS = 3000
MIN_REMAINING_MS = 20_000


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _collapsed(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalised_quote(value: Any) -> str:
    return _collapsed(value).translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})).casefold()


def _publisher(item: Any) -> str:
    publisher = _value(item, "publisher") or _value(item, "publisher_name")
    if publisher:
        return str(publisher)
    try:
        from catalyst_agents.attribution.quotes import _source_label
        return str(_source_label(item))
    except Exception:  # noqa: BLE001 — metadata is optional
        return "unknown publisher"


def _published_at(item: Any) -> Any:
    return _value(item, "eligible_at") or _value(item, "source_time_at") or _value(item, "published_at")


def _display_time(value: Any) -> str:
    if value is None:
        return "time unknown"
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


# Name words too common to identify a company on their own ("Bank", "General").
_GENERIC_NAME_WORDS = frozenset({
    "bank", "america", "american", "general", "motor", "motors", "platforms", "group", "holdings",
    "technology", "technologies", "international", "systems", "financial", "chase", "devices",
})
_NAME_ALIASES = {"GOOGL": ("google",), "GOOG": ("google",), "META": ("facebook",), "BRK.B": ("berkshire",)}
_EXCHANGE_TICKER = re.compile(r"\b(?:NYSE|NASDAQ|Nasdaq|NYSEARCA|AMEX)\s*:\s*([A-Z][A-Z.]{0,5})\b")


def names_company(quote: str, *, ticker: str, company_name: str) -> bool:
    """True when the quote names the company: its ticker in capitals, a distinctive name word, or every name word."""
    if re.search(rf"(?<![A-Za-z]){re.escape(ticker.upper())}(?![A-Za-z])", quote):
        return True
    words = set(re.findall(r"[a-z0-9]+", quote.casefold()))
    tokens = re.findall(r"[a-z0-9]+", company_name.casefold())
    tokens += list(_NAME_ALIASES.get(ticker.upper(), ()))
    if len(ticker) >= 4:  # "Meta" for META; short tickers (F, GS, NOW) count only in capitals
        tokens.append(ticker.casefold())
    distinctive = [t for t in tokens if len(t) >= 4 and t not in _GENERIC_NAME_WORDS]
    if any(t in words for t in distinctive):
        return True
    return bool(tokens) and all(t in words for t in tokens)


def other_stock_move(quote: str, *, ticker: str) -> bool:
    """True when the first listed company the quote tags is another one and it gives a % move ("Shares of Intel (NASDAQ:INTC) ... 14%")."""
    tagged = _EXCHANGE_TICKER.findall(quote)
    return bool(tagged) and tagged[0].upper() != ticker.upper() and "%" in quote


_STOP_WORDS = frozenset("""
that this with from have will been were their they them than then there these those about after before into over
under also more most such only some what when which while would could should said says year years company shares
stock stocks investors analyst analysts market markets trading today week report reported reports billion million
percent quarter""".split())
# Capitalised words that do not identify a story (titles, months, days, venues, rating words).
_PLAIN_CAPS = frozenset("""
ceo cfo the inc corp nasdaq nyse wall street january february march april may june july august september october
november december monday tuesday wednesday thursday friday eps neutral outperform underperform overweight
underweight buy hold sell perform equal-weight strong""".split())
EARLIER_STORY_LOOKBACK_DAYS = 14


def _story_words(text: str, exclude: set[str]) -> tuple[set[str], set[str]]:
    words, names = set(), set()
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9'&.-]*[A-Za-z0-9]", text or ""):
        word = re.sub(r"'s$", "", raw.casefold()).strip(".'")
        if len(word) < 4 or word in exclude or word in _STOP_WORDS:
            continue
        words.add(word)
        if raw[0].isupper() and word not in _PLAIN_CAPS:
            names.add(word)
    return words, names


def same_story(quote: str, headline: str, *, exclude: set[str]) -> bool:
    """True when a headline tells the quote's story: 4+ shared words, a shared name, and 2 names or 5 words."""
    quote_words, quote_names = _story_words(quote, exclude)
    shared = quote_words & _story_words(headline, exclude)[0]
    names = shared & quote_names
    return len(shared) >= 4 and bool(names) and (len(names) >= 2 or len(shared) >= 5)


def corpus_earlier_story(*, ticker: str, company_name: str, quote: str, before: Any, since: date
                         ) -> dict[str, Any] | None:
    """The first served article on the ticker, published in [since, before), that tells the quote's story.

    Read-only; any store problem returns None (the check is skipped, never fails the run).
    """
    try:
        from catalyst_data.config import db_path
        from catalyst_data.storage.connect import connect_readonly

        path = db_path()
        if not path.exists():
            return None
        exclude = {ticker.casefold(), *re.findall(r"[a-z0-9]+", company_name.casefold()),
                   *_NAME_ALIASES.get(ticker.upper(), ())}
        conn = connect_readonly(path, timeout_ms=5000, immutable=False)
        try:
            rows = conn.execute(
                "SELECT a.published_utc, a.title, coalesce(a.description, '') FROM article_tickers t "
                "JOIN articles a USING(article_id) WHERE t.ticker = ? AND t.reference_date BETWEEN ? AND ? "
                "AND a.published_utc >= ? AND a.published_utc < ? ORDER BY a.published_utc",
                (ticker.upper(), since.isoformat(), before.date().isoformat(), since.isoformat(),
                 before.strftime("%Y-%m-%dT%H:%M:%S")),
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — optional check
        return None
    for published, title, description in rows:
        if same_story(quote, f"{title} {description}", exclude=exclude):
            return {"published_at": published, "title": title}
    return None


_MOVE_WORDS = re.compile(
    r"\b(rose|rises?|risen|rising|fell|falls?|falling|jump(ed|s|ing)?|climb(ed|s|ing)?|surg(ed|es|ing)|soar(ed|s|ing)?|"
    r"drop(ped|s|ping)?|slid(es)?|slump(ed|s)?|gain(ed|s)?|rall(ied|ies|y)|sank|sink(s|ing)?|tumbl(ed|es|ing)|"
    r"plung(ed|es|ing)|declin(ed|es|ing)|advanc(ed|es|ing)|trad(ed|ing) (higher|lower)|(higher|lower) (on|in)|"
    r"(up|down) \d|added|lost|shed|pop(ped|s)?|spik(ed|es))\b", re.I)
_MOVE_SIZE = re.compile(r"\d+(\.\d+)?\s?%|\$\d")
_CAUSE_WORDS = re.compile(
    r"\b(after|as|amid|because|due to|thanks to|driven by|following|citing|while|when|said|says|announced|"
    r"report(ed|s)?|despite|ahead of|helped|led by|weigh(ed|s|ing)|lift(ed|s|ing)?|boost(ed|s|ing)?|sent|pushed|"
    r"dragged|hit by|fueled|spurred|triggered|news|in sympathy|coming from|powered by|caught in|according to|"
    r"reflects?|on the back of|in response to|in reaction to|tied to|linked to|with (traders|investors)|"
    r"on (reports?|hopes|fears|concerns|optimism|word|expectations|speculation|worries|strong|weak|solid|upbeat|"
    r"disappointing|better|worse)|from (a|an|the))\b", re.I)


def describes_move_only(quote: str) -> bool:
    """True for a sentence that gives the move's size but no cause ("Tesla stock jumped nearly 4% on Monday")."""
    return bool(_MOVE_WORDS.search(quote) and _MOVE_SIZE.search(quote) and not _CAUSE_WORDS.search(quote))


def _build_prompt(*, company_name: str, ticker: str, session: date, stock_pct: Any,
                  peers_pct: Any, sector_ticker: str | None, sector_pct: Any,
                  candidates: list[tuple[Any, str]]) -> str:
    weekday = session.strftime("%A")
    direction = "rose" if float(stock_pct or 0) >= 0 else "fell"
    articles = "\n\n".join(
        f"[{n}] { _publisher(item) } — {_display_time(_published_at(item))}\n{excerpt}"
        for n, (item, excerpt) in enumerate(candidates, 1)
    )
    return (
        f"{company_name} ({ticker}) {direction} {abs(float(stock_pct or 0)):.2f}% on {weekday} {session} (close to close). "
        f"Its peers' median move: {_pct(peers_pct)}. Sector ETF {sector_ticker or 'n/a'}: {_pct(sector_pct)}.\n\n"
        f"Below are news articles published around that session. Find the sentence that best states WHY {ticker} moved on {session}.\n"
        "Rules:\n- Copy the quote exactly, word for word, from one article.\n"
        f"- It must explain {ticker}'s move (or its group's move, if {ticker} moved with its group) in THIS session, not another day.\n"
        f"- The cause must have happened before the close of {session} (before the open, if the stock gapped at the open). News released after the previous session's close counts.\n"
        "- A sentence that only describes the move (size, direction, time) is not a reason. News the stock moved despite (\"despite\", \"shrugged off\") is not a reason.\n"
        "- Prefer a specific event (results, guidance, analyst action, deal, news report, a peer's results, macro data) over vague wording.\n"
        f"- A sentence about another company's own news or share move is a reason only if that company is a peer and {ticker} moved with its group (scope GROUP).\n"
        f"- A note or report written by {company_name} about markets or other companies is not news about {company_name}.\n"
        "- Background facts and older stories an article mentions in passing are not a reason. event_date is the day the event first became public, not the day the article was written.\n"
        "If no article states a reason, return found=false.\n\n"
        f"{articles}\n\nReturn JSON only: {{\"found\": true|false, \"article\": <number>, \"quote\": \"...\", \"cause\": \"short phrase, max 12 words\", \"scope\": \"COMPANY\"|\"GROUP\"|\"MARKET\", \"event_date\": \"YYYY-MM-DD\", \"event_timing\": \"BEFORE_OPEN\"|\"INTRADAY\"|\"AFTER_CLOSE\"|\"UNKNOWN\", \"confidence\": \"high\"|\"medium\"|\"low\"}}"
    )


def make_reported_reason(
    llm: Any, *, budget: Any = None, provider: str | None, model_id: str | None,
    deadline_epoch_ms: int | None,
    earlier_story: Callable[..., dict[str, Any] | None] | None = corpus_earlier_story,
) -> Callable[..., dict[str, Any] | None]:
    from catalyst_agents.runtime.provider_capability import (
        ModelSchemaFailure,
        ModelTransportFailure,
        TechnicalRetryExhausted,
        extract_provider_usage,
        invoke_with_bounded_retry,
        redact_provider_error,
    )

    def find(*, ticker: str, company_name: str, session_date: Any, stock_pct: Any,
             peers_pct: Any, sector_ticker: str | None, sector_pct: Any, items: Any
             ) -> dict[str, Any] | None:
        find.last_status = "none"
        find.last = None
        find.last_error = None
        if deadline_epoch_ms is not None and deadline_epoch_ms - time.time() * 1000 < MIN_REMAINING_MS:
            find.last_status = "skipped_near_deadline"
            return None
        try:
            session = _parse_date(session_date)
            if session is None:
                find.last_status = "bad_event_date"
                return None
            candidates = []
            for item in items or ():
                excerpt = _collapsed(_value(item, "excerpt_text"))[:1600]
                if excerpt:
                    candidates.append((item, excerpt))
                if len(candidates) == 40:
                    break
            if not candidates:
                find.last_status = "no_items"
                return None
            prompt = _build_prompt(company_name=company_name, ticker=ticker, session=session,
                                   stock_pct=stock_pct, peers_pct=peers_pct, sector_ticker=sector_ticker,
                                   sector_pct=sector_pct, candidates=candidates)
            binder = getattr(llm, "bind", None)
            # As the desk extractor: JSON mode, thinking off. With thinking on, reasoning used the whole
            # output cap and the reply had no JSON (MU 07-21, CRM 03-13: 42-47 s, schema failure).
            capped = (binder(max_tokens=MAX_OUTPUT_TOKENS, response_format={"type": "json_object"},
                             extra_body={"thinking": {"type": "disabled"}})
                      if callable(binder) else llm)

            def attempt() -> Any:
                try:
                    reply = capped.invoke(prompt)
                except Exception as exc:  # noqa: BLE001 — provider errors are transport failures
                    raise ModelTransportFailure(f"{type(exc).__name__}: {exc}"[:300]) from exc
                text = str(getattr(reply, "content", reply))
                try:
                    parsed = json.loads(text[text.find("{"):text.rfind("}") + 1])
                except (ValueError, TypeError) as exc:
                    raise ModelSchemaFailure(f"unparseable reported reason reply ({len(text)} chars)") from exc
                if not isinstance(parsed, dict) or not isinstance(parsed.get("found"), bool):
                    raise ModelSchemaFailure(f"reported reason found missing ({len(text)} chars)")
                return reply, parsed

            bounded = invoke_with_bounded_retry(
                attempt, role=REPORTED_REASON_ROLE,
                semantic_input_hash=hashlib.sha256(prompt.encode()).hexdigest(),
                budget=budget, provider=provider if budget is not None else None,
                model_id=model_id if budget is not None else None,
                usage_extractor=lambda result: extract_provider_usage(result[0]),
            )
            find.calls["logical_calls"] += bounded.counts.logical_calls
            find.calls["provider_attempts"] += bounded.counts.provider_attempts
            find.calls["attempt_usages"] += tuple(bounded.attempt_usages)
            parsed = bounded.result[1]
            if not parsed["found"]:
                find.last_status = "none"
                return None
            number = parsed.get("article")
            if isinstance(number, str) and number.strip().isdigit():
                number = int(number.strip())
            if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= len(candidates):
                find.last_status = "quote_not_verbatim"
                return None
            item, excerpt = candidates[number - 1]
            quote = _collapsed(parsed.get("quote"))
            if not quote or _normalised_quote(quote) not in _normalised_quote(excerpt):
                find.last_status = "quote_not_verbatim"
                return None
            scope = str(parsed.get("scope") or "COMPANY").upper()
            if scope == "COMPANY" and (not names_company(quote, ticker=ticker, company_name=company_name)
                                       or other_stock_move(quote, ticker=ticker)):
                find.last_status = "wrong_subject"
                return None
            if scope == "COMPANY" and describes_move_only(quote):
                find.last_status = "move_only"
                return None
            event_date = _parse_date(parsed.get("event_date"))
            if event_date is None:
                find.last_status = "bad_event_date"
                return None
            timing = str(parsed.get("event_timing") or "UNKNOWN").upper()
            if event_date > session:
                find.last_status = "bad_event_date"
                return None
            if event_date == session and timing == "AFTER_CLOSE":
                find.last_status = "after_close"
                return None
            from catalyst_data.trading_calendar import (
                latest_closed_trading_day_for_date,
            )
            previous = latest_closed_trading_day_for_date(session - timedelta(days=1))
            previous_date = _parse_date(previous)
            if previous_date is not None and event_date < previous_date:
                find.last_status = "stale_event"
                return None
            if scope == "COMPANY" and previous_date is not None and earlier_story is not None:
                # The model can date an old story to the session; the corpus says when it first ran.
                from zoneinfo import ZoneInfo
                before = datetime.combine(previous_date, dt_time(0, 0), ZoneInfo("America/New_York")
                                          ).astimezone(timezone.utc)
                if earlier_story(ticker=ticker, company_name=company_name, quote=quote, before=before,
                                 since=previous_date - timedelta(days=EARLIER_STORY_LOOKBACK_DAYS)):
                    find.last_status = "stale_story"
                    return None
            result = {
                "version": REPORTED_REASON_VERSION, "quote": quote,
                "cause": _collapsed(parsed.get("cause"))[:160],
                "scope": scope,
                "event_date": event_date.isoformat(), "event_timing": timing,
                "confidence": str(parsed.get("confidence") or "low").lower(),
                "evidence_id": _value(item, "evidence_id"), "publisher": _publisher(item),
                "published_at": _display_time(_published_at(item)),
                "url": _value(item, "canonical_url") or _value(item, "url"),
            }
            find.last_status = "found"
            find.last = result
            return result
        except TechnicalRetryExhausted as exc:
            find.calls["logical_calls"] += 1
            find.calls["provider_attempts"] += exc.attempts
            find.last_status = f"failed:{type(exc.last_error).__name__}"
            find.last_error = (
                f"{type(exc.last_error).__name__}: "
                f"{redact_provider_error(exc.last_error, getattr(llm, '_api_key', None))}"
            )
            return None

    find.last_status = "none"
    find.last = None
    find.last_error = None
    find.calls = {"logical_calls": 0, "provider_attempts": 0, "attempt_usages": ()}
    return find


def run_reported_reason_stage(*, trace: Any, enabled: bool, llm: Any, budget: Any,
                             provider: str | None, model_id: str | None, deadline_epoch_ms: int | None,
                             profile: Any, ticker: str, company_name: str, session_date: Any,
                             items: Any) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    """Run the optional reported-reason stage and record its trace payload."""
    result = None
    status = None
    if enabled:
        reason_stage = trace.begin("reported_reason")
        try:
            reason_finder = make_reported_reason(
                llm, budget=budget, provider=provider, model_id=model_id,
                deadline_epoch_ms=deadline_epoch_ms,
            )
            peer = getattr(getattr(profile, "peer_summary", None), "median_return_pct", None)
            reason_finder(
                ticker=ticker, company_name=company_name, session_date=session_date,
                stock_pct=getattr(profile, "target_return", None), peers_pct=peer,
                sector_ticker=getattr(profile, "sector_ticker", None),
                sector_pct=getattr(profile, "sector_return", None), items=items,
            )
            result = reason_finder.last
            status = reason_finder.last_status
            calls = dict(reason_finder.calls)
            trace.payload["reported_reason"] = {
                "status": status, "version": REPORTED_REASON_VERSION,
                **({"error": reason_finder.last_error} if getattr(reason_finder, "last_error", None) else {}),
            }
        except Exception as exc:  # noqa: BLE001 — this optional tier must not fail a run
            from catalyst_agents.runtime.provider_budget import ProviderBudgetError
            if isinstance(exc, ProviderBudgetError):
                raise
            status = f"failed:{type(exc).__name__}"
            calls = {"logical_calls": 0, "provider_attempts": 0, "attempt_usages": ()}
            trace.payload["reported_reason"] = {"status": status, "version": REPORTED_REASON_VERSION}
        trace.end(reason_stage)
    else:
        calls = {"logical_calls": 0, "provider_attempts": 0, "attempt_usages": ()}
    return result, status, calls


__all__ = ["MAX_OUTPUT_TOKENS", "REPORTED_REASON_ROLE", "REPORTED_REASON_VERSION", "make_reported_reason",
           "run_reported_reason_stage",
           "corpus_earlier_story", "describes_move_only", "names_company", "other_stock_move", "same_story"]
