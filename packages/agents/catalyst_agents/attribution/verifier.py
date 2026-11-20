"""Same-event verifier for a published PRIMARY (on by default; see attribution/policy.py).

One short model call reads the PRIMARY's rendered quotes (publisher and time
included) with the code-computed session facts and answers whether the quotes
give a reason THIS stock moved in THIS session. A REJECT lowers the status
ceiling to ABSTAIN; it can never raise a status or add a cause. The call goes
through the analyst's bounded retry and the shared provider budget (role
``primary_verifier``); it is skipped when less than MIN_REMAINING_MS of the run
deadline is left. No verdict (skipped, failed, unparseable) also abstains: an
unverified cause is never published. Fresh v3 acceptance (plan doc §16):
reference 0.551 -> 0.694, wrong-cause 0.356 -> 0.141.

Prompt v4 was chosen on graded v1/v2 answers; v5 only adds a category to
the reply (used to decide what an abstention may show); v6 counts a newly
disclosed investor/short-seller position or activist campaign as company news (scripts/m9_verify_probe.py):
wrong-cause rate 0.183 -> 0.086 (v2) and 0.188 -> 0.141 (v1) at a reference
cost of about -0.005 / -0.008. It was tuned on those sets; a fresh set decides.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from types import SimpleNamespace as NS
from typing import Any, Callable

from catalyst_agents.attribution._format import _pct

VERIFIER_VERSION = "primary_verifier_v7"
EVENT_TIME_VERIFIER_VERSION = "primary_verifier_event_time_v1"
EVENT_TIME_V2_VERIFIER_VERSION = "primary_verifier_event_time_v2"
EVENT_TIME_P1_VERIFIER_VERSION = "primary_verifier_event_time_p1"
VERIFIER_ROLE = "primary_verifier"
# The verifier reasons before it answers; reasoning counts toward output. The call is capped at this
# many tokens so the pre-call cost bound (pricing.json, primary_verifier) is a true upper bound. v7 asks for
# more judgments and hard cases ran out at 4000 with no JSON written (MU 04-28, 07-15: empty replies).
VERIFIER_MAX_OUTPUT_TOKENS = 8000
VERIFIER_REASONING_ENV = "CATALYST_VERIFIER_REASONING"


def verifier_reasoning_options() -> dict[str, Any]:
    """How much the verifier may reason (2026-11-18). ``off`` (the default) disables thinking: on 35 real
    verifier calls it agreed with thinking on 31 times, was judged right on 2 of the 3 disputed days, and
    took 1.4 s median / 3.3 s max instead of 5.0 s / 47 s. ``default`` keeps the provider's default
    thinking; ``low`` asks for low reasoning effort; ``budget:N`` caps thinking at N tokens."""
    import os

    value = os.environ.get(VERIFIER_REASONING_ENV, "off").strip().lower()
    if value == "default":
        return {}
    if value == "low":
        return {"reasoning_effort": "low"}
    if value.startswith("budget:") and value[7:].isdigit():
        return {"extra_body": {"thinking": {"type": "enabled", "budget_tokens": int(value[7:])}}}
    return {"extra_body": {"thinking": {"type": "disabled"}}}


# Pre-call input bound for the budget guard (prompt is ~1.5k tokens).
VERIFIER_MAX_INPUT_TOKENS = 6_000
MIN_REMAINING_MS = 8_000

PROMPT = """You check one published answer of a stock-move attribution system.

Session: {ticker} on {date} (US regular session; opened {open_at}, closed {close_at}).
Observed: {ticker} {ret:+.2f}% close to close; opening gap {gap}; S&P 500 {spy}; sector ETF {etf}; commodity proxy {commodity}.
Co-movement computed from closing prices: {comove}

The system's main cause, as verbatim quotes with publisher and publication time:
{primary}

Question: do the quotes give a reason THIS stock moved in THIS session (after the previous close, up to this close)?
Default to REJECT unless the quotes give a reason this stock moved this session. They do if either:
A. they report company news about this stock from this window (results, guidance, deal, rating change, regulatory/legal action, a material operational event, or a notable investor's or short seller's newly disclosed position, short report or activist campaign in this stock) — even if the stock moved against the headline numbers, when the quotes say what investors reacted to instead (e.g. results beat but guidance disappointed); or
B. they report what moved this stock's market, sector, peer group or commodity this session, AND the observed numbers show this stock moved in the same direction as that group. Same direction is enough: a stock that fell 5% on a day the market fell 1.5% moved with the market (a larger or smaller move is fine). The quotes need not name this stock.
REJECT in particular when the quotes recap another period, preview something not yet happened, are evergreen commentary (valuation, brand history, long-term thesis), are about another company with no stated link, or are a same-day story about a group this stock moved against.
Also REJECT when the quotes only describe the move (direction, size, time of day) without saying what caused it, or when the only news they name is news the stock moved despite ("despite", "even though", "shrugged off") with nothing else that moved it.
Otherwise answer SUPPORTED.
Also give a category: for SUPPORTED, COMPANY_NEWS or GROUP_DRIVER; for REJECT, the main reason: RECAP (another period), PREVIEW (not yet happened), EVERGREEN (valuation, history, long-term thesis), OTHER_COMPANY (no stated link), AGAINST_GROUP (this stock moved against that group), MOVE_ONLY (only describes the move), MOVED_DESPITE (the stock moved despite the news), or UNLINKED (news from this session about this stock or its group, but nothing shows it moved the stock).
For SUPPORTED, also give cause_quote: the number of the quote that states the reason.
Return JSON: {{"verdict": "SUPPORTED" or "REJECT", "category": "...", "cause_quote": 1, "reason": "one sentence"}}"""

EVENT_TIME_PROMPT = PROMPT.replace(
    "Otherwise answer SUPPORTED.",
    "Otherwise answer SUPPORTED. Under event_time_v1, also REJECT a recap that names no concrete event as RECAP_WITHOUT_EVENT, and reject a hedged causal link with no strong, concrete event as HEDGED_LINK. These categories are reject-only; never use them for SUPPORTED.",
).replace(
    "MOVE_ONLY (only describes the move), MOVED_DESPITE (the stock moved despite the news), or UNLINKED (news from this session about this stock or its group, but nothing shows it moved the stock).",
    "or UNLINKED (news from this session about this stock or its group, but nothing shows it moved the stock), RECAP_WITHOUT_EVENT, or HEDGED_LINK.",
)
EVENT_TIME_V2_PROMPT = EVENT_TIME_PROMPT.replace("Under event_time_v1", "Under event_time_v2")
# event_time_p1 keeps P's verifier but its sources may be published up to three trading days
# after the session: a later article explaining THIS session's move is not a recap (v6 dev:
# the P prompt rejected MSFT/MU/EOG next-day explanations by their publication date).
EVENT_TIME_P1_PROMPT = PROMPT.replace(
    "Question: do the quotes give a reason THIS stock moved in THIS session",
    "Sources may be published up to three trading days after this session. Judge the session a quote "
    "describes, not its publication time: a later article explaining why this stock moved on {date} "
    "(\"shares fell Monday after...\" where Monday is {date}) counts, and an event announced before the "
    "close counts. RECAP means the quote describes a move or event in a different session.\n\n"
    "Question: do the quotes give a reason THIS stock moved in THIS session",
)


def comove_text(observation: Any) -> str:
    from catalyst_agents.attribution.session_facts import _session_facts

    profile = NS(
        market_comove=getattr(observation, "market_comove", None),
        sector_comove=getattr(observation, "sector_comove", None),
        peer_comove=getattr(observation, "peer_comove", None),
        commodity_ticker=getattr(observation, "commodity_ticker", None),
        commodity_return=getattr(observation, "commodity_return", None),
        target_return=getattr(observation, "target_return", None),
        gap_return=None,
    )
    return _session_facts(profile, NS(evidence_inventory=()), None) or "unavailable"


_QUOTE_ITEM = re.compile(r"“.*?” — [^“\n]*", re.S)


def quote_lines(statement: str) -> list[str]:
    """The rendered quotes of a statement (“text” — publisher, time, url), as numbered in the prompt.

    Statements put every quote on one line, separated by a space; a quote's own text may contain quotation marks,
    so an item ends only where its “” — publisher” suffix runs into the next opening mark."""
    items = [m.group(0).strip() for m in _QUOTE_ITEM.finditer(statement)]
    return items or [line for line in statement.split("\n") if line.strip()]


def quoted_text(line: str) -> str:
    """The verbatim sentence of one rendered quote line (“text” — publisher, time)."""
    start, end = line.find("“"), line.rfind("” —")
    return line[start + 1:end] if start >= 0 and end > start else line.strip()


def build_prompt(
    statement: str, context_pack: Any, *, evidence_window_policy: str = "pre_close_v1",
) -> str | None:
    o = context_pack.observation
    t = context_pack.temporal_identity
    ret = getattr(o, "target_return", None)
    if ret is None:
        return None
    market = getattr(getattr(o, "market_comove", None), "reference_return_pct", None)
    sector = getattr(getattr(o, "sector_comove", None), "reference_return_pct", None)
    commodity = getattr(o, "commodity_ticker", None)
    template = (
        EVENT_TIME_V2_PROMPT if evidence_window_policy == "event_time_v2"
        else EVENT_TIME_PROMPT if evidence_window_policy == "event_time_v1"
        else EVENT_TIME_P1_PROMPT if evidence_window_policy == "event_time_p1"
        else PROMPT
    )
    verified_release_note = ""
    if re.search(r"—\s*SEC filing, (?:pre|intraday|post) release \d{4}-\d{2}-\d{2}T", statement):
        verified_release_note = (
            "\n\nCode-verified issuer earnings release timing is attached to the marked SEC quote. "
            "A pre-market or intraday release is assigned to this session; a post-close release is "
            "assigned to the next trading session. An intraday release may explain the full daily "
            "close-to-close bar. Apply this only to the marked quote; the marker does not prove the cause."
        )
    return template.format(
        ticker=getattr(o, "ticker", None) or "the stock", date=t.session_date,
        open_at=t.session_open_at.isoformat(), close_at=t.session_close_at.isoformat(),
        ret=ret, gap=_pct(getattr(o, "gap_return", None)), spy=_pct(market),
        etf=f"{getattr(o, 'sector_ticker', None) or 'n/a'} {_pct(sector)}",
        commodity=f"{commodity} {_pct(getattr(o, 'commodity_return', None))}" if commodity else "n/a",
        comove=comove_text(o), primary="\n".join(f"{n}. {line}" for n, line in enumerate(quote_lines(statement), 1))
        + verified_release_note,
    )


def make_verifier(
    llm: Any,
    *,
    budget: Any | None = None,
    provider: str | None = None,
    model_id: str | None = None,
    deadline_epoch_ms: int | None = None,
    evidence_window_policy: str = "pre_close_v1",
) -> Callable[[Any, Any], tuple[bool | None, str]]:
    """A verifier over ``llm`` (a chat model with ``invoke``).

    Returns ``(True, reason)`` supported, ``(False, reason)`` rejected, or
    ``(None, why)`` when no verdict was obtained. A provider budget that is
    exhausted propagates, as it does for the Analyst.
    """
    from catalyst_agents.runtime.provider_capability import (
        ModelSchemaFailure,
        ModelTransportFailure,
        TechnicalRetryExhausted,
        extract_provider_usage,
        invoke_with_bounded_retry,
    )

    def verify(primary: Any, context_pack: Any) -> tuple[bool | None, str]:
        verify.last = None  # never carry a previous call's verdict into this one
        prompt = build_prompt(
            primary.statement, context_pack,
            evidence_window_policy=evidence_window_policy,
        )
        if prompt is None:
            return None, "no_target_return"
        if deadline_epoch_ms is not None and deadline_epoch_ms - time.time() * 1000 < MIN_REMAINING_MS:
            return None, "skipped_near_deadline"

        binder = getattr(llm, "bind", None)
        capped = (binder(max_tokens=VERIFIER_MAX_OUTPUT_TOKENS, **verifier_reasoning_options())
                  if callable(binder) else llm)

        def attempt() -> Any:
            try:
                reply = capped.invoke(prompt)
            except Exception as exc:  # noqa: BLE001 — provider errors are transport failures
                raise ModelTransportFailure(f"{type(exc).__name__}: {exc}"[:300]) from exc
            text = str(getattr(reply, "content", reply))
            try:
                parsed = json.loads(text[text.find("{"): text.rfind("}") + 1])
            except ValueError as exc:
                raise ModelSchemaFailure(f"unparseable verifier reply ({len(text)} chars)") from exc
            if parsed.get("verdict") not in ("SUPPORTED", "REJECT"):
                raise ModelSchemaFailure(f"verifier verdict missing ({len(text)} chars)")
            return reply, parsed

        try:
            bounded = invoke_with_bounded_retry(
                lambda: attempt(),
                role=VERIFIER_ROLE,
                semantic_input_hash=hashlib.sha256(prompt.encode()).hexdigest(),
                budget=budget,
                provider=provider if budget is not None else None,
                model_id=model_id if budget is not None else None,
                usage_extractor=lambda result: extract_provider_usage(result[0]),
            )
        except TechnicalRetryExhausted as exc:
            verify.calls["logical_calls"] += 1
            verify.calls["provider_attempts"] += exc.attempts
            return None, f"failed:{type(exc.last_error).__name__}: {exc.last_error}"[:200]
        verify.calls["logical_calls"] += bounded.counts.logical_calls
        verify.calls["provider_attempts"] += bounded.counts.provider_attempts
        verify.calls["attempt_usages"] += tuple(bounded.attempt_usages)
        parsed = bounded.result[1]
        reason = " ".join(str(parsed.get("reason", "")).split())[:200]
        category = str(parsed.get("category", "")).strip().upper()[:20] or "NONE"
        verdict = parsed["verdict"] == "SUPPORTED"
        # Kept for an abstention's display (abstain_context.py); never a cause.
        # v7: the quote the verifier says states the reason; the answer leads with it (a sentence that only
        # describes the move is never the headline quote).
        lines = quote_lines(primary.statement)
        number = parsed.get("cause_quote")
        if isinstance(number, list) and len(number) == 1:
            number = number[0]  # the model sometimes answers [2] or "2"
        if isinstance(number, str) and number.strip().isdigit():
            number = int(number.strip())
        cause_quote = (quoted_text(lines[number - 1]) if verdict and isinstance(number, int)
                       and not isinstance(number, bool) and 1 <= number <= len(lines) else None)
        verify.last = {"statement": primary.statement, "verdict": verdict,
                       "category": category, "reason": reason, "cause_quote": cause_quote}
        return verdict, f"{category}:{reason}"

    verify.last = None
    # Run accounting, read by the graph into its call and token totals.
    verify.calls = {"logical_calls": 0, "provider_attempts": 0, "attempt_usages": ()}
    return verify


__all__ = ["quote_lines", "quoted_text", "EVENT_TIME_P1_PROMPT", "EVENT_TIME_P1_VERIFIER_VERSION", "EVENT_TIME_PROMPT", "EVENT_TIME_V2_PROMPT", "EVENT_TIME_VERIFIER_VERSION", "EVENT_TIME_V2_VERIFIER_VERSION", "VERIFIER_ROLE", "VERIFIER_VERSION", "build_prompt", "make_verifier"]
