"""Same-event verifier: verdicts, bounded retry, deadline skip; no verdict means abstain."""
import time
from datetime import datetime, timezone
from types import SimpleNamespace as NS

from catalyst_agents.attribution.verifier import (
    EVENT_TIME_PROMPT,
    PROMPT,
    build_prompt,
    make_verifier,
    quote_lines,
)

UTC = timezone.utc
PACK = NS(
    observation=NS(ticker="XOM", target_return=2.36, gap_return=0.59, market_comove=None,
                   sector_comove=None, peer_comove=None, sector_ticker="XLE",
                   commodity_ticker="USO", commodity_return=0.06),
    temporal_identity=NS(session_date="2026-02-23",
                         session_open_at=datetime(2026, 2, 23, 14, 30, tzinfo=UTC),
                         session_close_at=datetime(2026, 2, 23, 21, 0, tzinfo=UTC)),
)
PRIMARY = NS(statement="“Brent rose 17% in January.” — The Motley Fool, 2026-02-22 15:25 UTC, https://x")


class FakeLLM:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0

    def invoke(self, prompt):
        self.calls += 1
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return NS(content=reply)


def test_prompt_carries_the_session_and_quotes():
    prompt = build_prompt(PRIMARY.statement, PACK)
    assert "XOM on 2026-02-23" in prompt and "+2.36%" in prompt and "Brent rose 17% in January" in prompt


def test_event_time_verifier_prompt_adds_reject_only_categories():
    default = build_prompt(PRIMARY.statement, PACK)
    event_time = build_prompt(PRIMARY.statement, PACK, evidence_window_policy="event_time_v1")
    assert "RECAP_WITHOUT_EVENT" not in default and "HEDGED_LINK" not in default
    assert "RECAP_WITHOUT_EVENT" in event_time and "HEDGED_LINK" in event_time
    assert PROMPT != EVENT_TIME_PROMPT


def test_verdicts_and_reasons():
    verify = make_verifier(FakeLLM('{"verdict": "REJECT", "category": "RECAP", "reason": "a January recap"}'))
    assert verify(PRIMARY, PACK) == (False, "RECAP:a January recap")
    assert verify.last == {"statement": PRIMARY.statement, "verdict": False, "category": "RECAP",
                           "reason": "a January recap", "cause_quote": None}
    assert make_verifier(FakeLLM('ok {"verdict": "SUPPORTED", "reason": "x"}'))(PRIMARY, PACK) == (True, "NONE:x")


def test_one_technical_retry_then_no_verdict():
    llm = FakeLLM("not json", '{"verdict": "SUPPORTED", "reason": "y"}')
    assert make_verifier(llm)(PRIMARY, PACK) == (True, "NONE:y") and llm.calls == 2
    llm = FakeLLM(TimeoutError("slow"))
    verdict, why = make_verifier(llm)(PRIMARY, PACK)
    assert verdict is None and why.startswith("failed:") and llm.calls == 2


def test_skipped_near_the_deadline():
    llm = FakeLLM('{"verdict": "SUPPORTED", "reason": "x"}')
    deadline = int(time.time() * 1000) + 2_000
    assert make_verifier(llm, deadline_epoch_ms=deadline)(PRIMARY, PACK) == (None, "skipped_near_deadline")
    assert llm.calls == 0


def test_the_call_is_capped_at_the_priced_output_bound():
    # 2026-11-16: a second, stale 400-token constant overrode the cap; the reasoning reply was cut off and
    # 14 of 21 verifier calls returned no verdict, which turned published answers into abstentions.
    import json
    from pathlib import Path

    class Bindable(FakeLLM):
        bound = None

        def bind(self, **kwargs):
            Bindable.bound = kwargs
            return self

    pricing = Path(__file__).parents[2] / "api/catalyst_app/runtime/pricing.json"
    limit = json.loads(pricing.read_text())["role_token_limits"]["primary_verifier"]["max_output_tokens"]
    make_verifier(Bindable('{"verdict": "SUPPORTED", "reason": "x"}'))(PRIMARY, PACK)
    assert Bindable.bound["max_tokens"] == limit and limit >= 4000
    # 2026-11-18: thinking is off by default (CATALYST_VERIFIER_REASONING)
    assert Bindable.bound.get("extra_body") == {"thinking": {"type": "disabled"}}


# As the analyst renders it: every quote on one line (MU 04-28).
TWO = NS(statement="“Micron is down 2.6% at 10 a.m.” — The Motley Fool, 2026-04-28 14:38 UTC, https://f.com/a "
                   "“The Journal reports OpenAI missed its targets.” — The Motley Fool, 2026-04-28 14:38 UTC, https://f.com/a")


def test_quotes_are_numbered_and_the_cause_quote_is_returned():
    prompt = build_prompt(TWO.statement, PACK)
    assert "1. “Micron is down" in prompt and "\n2. “The Journal" in prompt and "MOVE_ONLY" in prompt
    assert quote_lines("“He said “tough” times.” — Fool, t, u “B.” — X, t") == ["“He said “tough” times.” — Fool, t, u", "“B.” — X, t"]
    verify = make_verifier(FakeLLM('{"verdict": "SUPPORTED", "category": "GROUP_DRIVER", "cause_quote": 2, "reason": "r"}'))
    assert verify(TWO, PACK)[0] is True
    assert verify.last["cause_quote"] == "The Journal reports OpenAI missed its targets."
    for good in ('"2"', "[2]"):
        verify = make_verifier(FakeLLM('{"verdict": "SUPPORTED", "cause_quote": %s, "reason": "r"}' % good))
        verify(TWO, PACK)
        assert verify.last["cause_quote"] == "The Journal reports OpenAI missed its targets."
    for bad in ("0", "3", "true", '"two"'):
        verify = make_verifier(FakeLLM('{"verdict": "SUPPORTED", "cause_quote": %s, "reason": "r"}' % bad))
        verify(TWO, PACK)
        assert verify.last["cause_quote"] is None
    verify = make_verifier(FakeLLM('{"verdict": "REJECT", "category": "MOVE_ONLY", "cause_quote": 1, "reason": "r"}'))
    assert verify(TWO, PACK) == (False, "MOVE_ONLY:r") and verify.last["cause_quote"] is None
