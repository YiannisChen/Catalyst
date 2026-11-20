from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from catalyst_agents.attribution.extraction import (
    AboutTicker,
    EventCluster,
    EventTiming,
    EventType,
    Extraction,
    GateConfig,
    Session,
    attach_quote_roles,
    cluster_events,
    parse_extraction,
    render_statement,
    select_and_gate,
    validate_quotes,
)
from catalyst_data.canonical.temporal import build_temporal_identity


def _payload(**overrides):
    data = {
        "about_ticker": "ISSUER", "session": "THIS_SESSION", "event_type": "EARNINGS",
        "event_summary": "company cuts full year outlook", "event_timing": "INTRADAY",
        "explicit_attribution": True,
        "attribution_quote": "Shares of ACME fell 4% after ACME cut its full year outlook sharply.",
        "event_quote": "ACME cut its full year outlook sharply after reporting quarterly results.",
        "mechanism_quote": "",
        "is_preview_or_evergreen": False,
    }
    return {**data, **overrides}


def _item(evidence_id="e1", **overrides):
    return {
        "evidence_id": evidence_id, "content_state": "FULL_TEXT",
        "eligible_at": "2026-04-08T19:00:00Z", "publisher": "Example News",
        "source_class": "reported_news", "canonical_url": "https://example.com/story",
        "excerpt_text": (
            "Shares of ACME fell 4% after ACME cut its full year outlook sharply. "
            "ACME cut its full year outlook sharply after reporting quarterly results."
        ),
        "independence_group_id": evidence_id,
        **overrides,
    }


def _ext(**overrides):
    return parse_extraction(json.dumps(_payload(**overrides)))


def test_parse_extraction_valid_invalid_and_enum_errors():
    parsed = parse_extraction(json.dumps(_payload()))
    assert parsed.valid and parsed.event_type == EventType.EARNINGS
    assert not parse_extraction("not json").valid
    assert not parse_extraction(json.dumps(_payload(extra="x"))).valid
    assert not parse_extraction(json.dumps(_payload(event_type="MAGIC"))).valid


def test_validate_quotes_blanks_nonverbatim_and_r6_other_session_move():
    temporal = build_temporal_identity("2026-04-08")
    ext = _ext(attribution_quote="This invented sentence is not in the excerpt.")
    checked = validate_quotes(ext, _item(), temporal, "pre_close_v1")
    assert checked.attribution_quote == ""
    assert not checked.explicit_attribution
    assert "R2_NOT_VERBATIM" in checked.quote_failures[0]

    other = "Shares fell Tuesday as ACME shares slid 4% after a large forecast cut."
    with_other_day = _item(excerpt_text=other)
    checked_other = validate_quotes(_ext(attribution_quote=other, event_quote=other), with_other_day,
                                    temporal, "pre_close_v1")
    assert checked_other.attribution_quote == ""
    assert "R6_OTHER_SESSION_MOVE" in " ".join(checked_other.quote_failures)


def test_cluster_merges_syndication_earnings_and_preserves_event_types():
    first = _item("e1", dedup_cluster_id="wire-copy", publisher="Yahoo Finance",
                  canonical_url="https://finance.yahoo.com/a")
    copy = _item("e2", dedup_cluster_id="wire-copy", publisher="Yahoo Finance",
                 canonical_url="https://finance.yahoo.com/b")
    later = _item("e3", dedup_cluster_id="origin-3", publisher="Wire",
                  canonical_url="https://wire.example/a")
    clusters = cluster_events([(first, _ext()), (copy, _ext()),
                               (later, _ext(event_summary="earnings release and outlook change", event_quote=_payload()["event_quote"]))])
    assert len(clusters) == 1
    assert clusters[0].independent_origins == 2
    assert clusters[0].event_type == EventType.EARNINGS

    different = _item("e4", publisher="Other", canonical_url="https://other.example/a")
    split = cluster_events([(first, _ext()), (different, _ext(event_type="ANALYST_ACTION",
                       event_summary="analyst downgrades shares"))])
    assert len(split) == 2


def _cluster(event_type, *, scope=AboutTicker.ISSUER, explicit=0, primary=False, event=True, when="2026-04-08T19:00:00Z"):
    items = []
    members = []
    for n in range(max(1, explicit, int(primary))):
        item = _item(f"e{n}", eligible_at=when, source_class="issuer_disclosure" if primary and n == 0 else "reported_news")
        ext = Extraction(about_ticker=scope, session=Session.THIS_SESSION, event_type=event_type,
                         event_summary="a cause", explicit_attribution=n < explicit,
                         attribution_quote="exact attribution" if n < explicit else "",
                         event_quote="exact event" if event else "")
        members.append((item, ext))
    return EventCluster(event_type, tuple(members), len(members), explicit, primary, scope,
                        (members[0][0], "exact attribution") if explicit else None,
                        (members[0][0], "exact event") if event else None)


def test_group_consistency_and_gate_variants_with_competing_causes():
    group = _cluster(EventType.MACRO, scope=AboutTicker.GROUP, explicit=1)
    facts = {"observation": {"market_comove": {"direction_relation": "OPPOSITE_DIRECTION"},
                             "sector_comove": {"direction_relation": "UNKNOWN"}}}
    assert not select_and_gate([group], facts, GateConfig("standard")).publish
    facts["observation"]["market_comove"]["direction_relation"] = "SAME_DIRECTION"
    assert select_and_gate([group], facts, GateConfig("standard")).publish

    cause_a = _cluster(EventType.EARNINGS, explicit=1, primary=True)
    cause_b = _cluster(EventType.ANALYST_ACTION, explicit=1, primary=True, when="2026-04-08T20:00:00Z")
    assert select_and_gate([cause_a, cause_b], {}, GateConfig("strict")).reason_code == "COMPETING_CAUSES"
    standard = select_and_gate([cause_a, cause_b], {}, GateConfig("standard"))
    assert standard.publish and standard.primary is cause_a
    no_explicit = _cluster(EventType.OTHER, explicit=0)
    assert not select_and_gate([no_explicit], {}, GateConfig("strict")).publish
    assert not select_and_gate([no_explicit], {}, GateConfig("standard")).publish
    assert select_and_gate([no_explicit], {}, GateConfig("nogate")).publish


def test_render_statement_quotes_source_and_time_and_caps_length():
    cluster = _cluster(EventType.EARNINGS, explicit=1)
    statement = render_statement(cluster)
    assert statement.startswith("“exact attribution” — Example News, 2026-04-08 19:00 UTC")
    assert "“exact event”" in statement
    huge_item = _item(publisher="P" * 1000)
    huge = EventCluster(cluster.event_type, ((huge_item, cluster.members[0][1]),), 1, 1, False,
                        AboutTicker.ISSUER, (huge_item, "Q" * 900), (huge_item, "E" * 900))
    rendered = render_statement(huge)
    # Each quote stays whole and at most 900 chars; quote text totals at most 1,800 chars.
    assert "Q" * 900 in rendered and "E" * 900 in rendered and rendered.count("“") == 2
    over = EventCluster(cluster.event_type, ((huge_item, cluster.members[0][1]),), 1, 1, False,
                        AboutTicker.ISSUER, (huge_item, "Q" * 901), (huge_item, "E" * 10))
    assert "Q" * 901 not in render_statement(over)


class _FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, *, messages, **kwargs):
        user = messages[-1]["content"]
        if "EVENT_TIME_P1_PROMPT" in user or "Question: do the quotes give a reason" in user:
            body = {"verdict": "SUPPORTED", "category": "COMPANY_NEWS", "reason": "quoted event"}
        else:
            body = _payload()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(body)))],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30))


def _desk_cluster(event_type, scope, *, explicit=0, primary=False, origins=1, timings=("BEFORE_OPEN",)):
    from catalyst_agents.attribution.extraction import EventCluster, EventTiming
    item = {"evidence_id": f"{event_type}-{scope}", "eligible_at": "2026-04-08T14:00:00Z", "publisher": "P"}
    return EventCluster(event_type, ((item, None),), origins, explicit, primary, scope,
                        (item, "X rose after Y.") if explicit else None, (item, "Y happened."),
                        True, frozenset(EventTiming(t) for t in timings))


def test_desk_gate_inference_paths_and_ranking():
    from catalyst_agents.attribution.extraction import (
        AboutTicker,
        EventType,
        GateConfig,
        select_and_gate,
    )
    moved_with_market = {"observation": {"market_comove": {"direction_relation": "SAME_DIRECTION", "band": "ALIGNED"},
                                         "sector_comove": {"direction_relation": "SAME_DIRECTION", "band": "ALIGNED"}}}
    bigger_than_market = {"observation": {"market_comove": {"direction_relation": "SAME_DIRECTION", "band": "DIVERGENT"},
                                          "sector_comove": {"direction_relation": "SAME_DIRECTION", "band": "DIVERGENT"}}}
    idiosyncratic = {"observation": {"market_comove": {"direction_relation": "OPPOSITE_DIRECTION"},
                                     "sector_comove": {"direction_relation": "OPPOSITE_DIRECTION"}}}
    desk = GateConfig("desk")
    # A confirmed 8-K event before the open is publishable as an inferred link; standard abstains.
    filing = _desk_cluster(EventType.DEAL_MA, AboutTicker.ISSUER, primary=True)
    assert select_and_gate([filing], idiosyncratic, desk).basis == "CONFIRMED_EVENT_INFERRED_LINK"
    assert not select_and_gate([filing], idiosyncratic, GateConfig("standard")).publish
    # A single-origin, non-primary company event is not enough.
    assert not select_and_gate([_desk_cluster(EventType.EARNINGS, AboutTicker.ISSUER)], idiosyncratic, desk).publish
    # An after-close event cannot explain the session.
    late = _desk_cluster(EventType.EARNINGS, AboutTicker.ISSUER, primary=True, timings=("AFTER_CLOSE",))
    assert not select_and_gate([late], idiosyncratic, desk).publish
    # A group driver needs a source stating why the group moved, and a stock that tracked the group.
    macro = _desk_cluster(EventType.MACRO, AboutTicker.GROUP, explicit=1)
    assert select_and_gate([macro], moved_with_market, desk).basis == "GROUP_DRIVER"
    assert not select_and_gate([_desk_cluster(EventType.MACRO, AboutTicker.GROUP)], moved_with_market, desk).publish
    assert not select_and_gate([macro], idiosyncratic, desk).publish
    # Same direction but a much larger move: the group does not explain it.
    assert not select_and_gate([macro], bigger_than_market, desk).publish
    # A reported attribution outranks an inferred one.
    reported = _desk_cluster(EventType.ANALYST_ACTION, AboutTicker.ISSUER, explicit=1)
    decision = select_and_gate([macro, reported], moved_with_market, desk)
    # The stock tracked its group, so the stated group driver leads and the company report is secondary.
    assert decision.basis == "GROUP_DRIVER" and decision.primary is macro and decision.secondary is reported
    # Two confirmed company events and no source stating which one moved the stock: abstain.
    other = _desk_cluster(EventType.MANAGEMENT, AboutTicker.ISSUER, primary=True)
    assert not select_and_gate([filing, other], idiosyncratic, desk).publish


def test_x58_like_lead_attribution_with_same_source_event_is_rejected():
    from catalyst_agents.engine import gate as engine_gate

    event_quote = "PayPal (PYPL) announced it would integrate Pix into PayPal Complete Payments."
    attribution_quote = "PayPal shares fell 4% amid broader concerns about the payments business."
    item = _item("x58", excerpt_text=f"{attribution_quote} {event_quote}")
    temporal = build_temporal_identity("2026-04-08")
    extraction = Extraction(
        about_ticker=AboutTicker.ISSUER, session=Session.THIS_SESSION,
        event_type=EventType.PRODUCT_OPERATIONS, event_summary="PayPal announces Pix integration",
        event_timing=EventTiming.INTRADAY, explicit_attribution=True,
        attribution_quote=attribution_quote, event_quote=event_quote,
    )
    checked = attach_quote_roles(
        validate_quotes(extraction, item, temporal, "pre_close_v1"), item, temporal, "pre_close_v1",
        ticker="PYPL", move_profile=SimpleNamespace(ticker="PYPL", target_return=-4.0),
    )
    assert checked.attribution_roles == ("LEAD",)
    assert checked.event_roles == ("EVENT",)
    cluster = cluster_events([(item, checked)])[0]
    assert cluster.best_attribution_roles == ("LEAD",)
    assert cluster.best_event_roles == ("EVENT",)

    facts = {"observation": {"market_comove": {"direction_relation": "OPPOSITE_DIRECTION"},
                             "sector_comove": {"direction_relation": "OPPOSITE_DIRECTION"}}}
    for gate in (select_and_gate, engine_gate.select_and_gate):
        decision = gate([cluster], facts, GateConfig("desk"))
        assert not decision.publish and decision.reason_code == "NO_ELIGIBLE_CAUSE"
        assert decision.diagnostics[0]["pairing_rule"] == "rejected_lead_same_source_event"
        assert decision.diagnostics[0]["best_attribution"]["roles"] == ["LEAD"]
    assert not engine_gate.gate([cluster], facts).publish
    assert engine_gate.gate([cluster], facts).diagnostics[0]["pairing_rule"] == "rejected_lead_same_source_event"


def _role_cluster(*, attribution_item, event_item, attribution_quote, event_quote,
                  attribution_roles, event_roles):
    return EventCluster(
        EventType.PRODUCT_OPERATIONS,
        ((attribution_item, None), (event_item, None)),
        2, 1, False, AboutTicker.ISSUER,
        (attribution_item, attribution_quote), (event_item, event_quote),
        True, frozenset({EventTiming.INTRADAY}),
        best_attribution_roles=attribution_roles, best_event_roles=event_roles,
    )


def test_lead_attribution_with_event_from_different_source_keeps_previous_behavior():
    from catalyst_agents.engine import gate as engine_gate

    facts = {"observation": {"market_comove": {"direction_relation": "OPPOSITE_DIRECTION"},
                             "sector_comove": {"direction_relation": "OPPOSITE_DIRECTION"}}}
    cluster = _role_cluster(
        attribution_item=_item("a", publisher="One"),
        event_item=_item("b", publisher="Two"),
        attribution_quote="PayPal shares moved on the Pix news.",
        event_quote="PayPal announced it would integrate Pix.",
        attribution_roles=("LEAD",), event_roles=("EVENT",),
    )
    for gate in (select_and_gate, engine_gate.select_and_gate):
        decision = gate([cluster], facts, GateConfig("desk"))
        assert decision.publish and decision.basis == "REPORTED_ATTRIBUTION"
        assert decision.diagnostics[0]["pairing_rule"] == "passed"
    assert engine_gate.gate([cluster], facts).publish


def test_desk_cluster_trace_records_pairing_roles_and_hashes_without_quote_text():
    from catalyst_agents.nodes.desk_decision import _cluster_trace_rows

    facts = {"observation": {"market_comove": {"direction_relation": "OPPOSITE_DIRECTION"},
                             "sector_comove": {"direction_relation": "OPPOSITE_DIRECTION"}}}
    attr_quote = "PayPal shares fell after concerns about payments."
    event_quote = "PayPal announced it would integrate Pix."
    rejected = _role_cluster(
        attribution_item=_item("x58"), event_item=_item("x58"),
        attribution_quote=attr_quote, event_quote=event_quote,
        attribution_roles=("LEAD",), event_roles=("EVENT",),
    )
    passed = _role_cluster(
        attribution_item=_item("ev-a"), event_item=_item("ev-b"),
        attribution_quote=attr_quote, event_quote=event_quote,
        attribution_roles=("LEAD",), event_roles=("EVENT",),
    )
    clusters = [rejected, passed]
    gate = select_and_gate(clusters, facts, GateConfig("desk"))

    rows = _cluster_trace_rows(clusters, gate.diagnostics)

    assert rows[0]["best_attribution"] == {
        "quote_sha256": hashlib.sha256(attr_quote.encode()).hexdigest(),
        "evidence_id": "x58", "roles": ["LEAD"],
    }
    assert rows[0]["best_event"] == {
        "quote_sha256": hashlib.sha256(event_quote.encode()).hexdigest(),
        "evidence_id": "x58", "roles": ["EVENT"],
    }
    assert rows[0]["pairing_rule"] == "rejected_lead_same_source_event"
    assert rows[1]["pairing_rule"] == "passed"
    assert attr_quote not in json.dumps(rows) and event_quote not in json.dumps(rows)


def test_non_lead_attribution_is_not_checked_for_concrete_words():
    from catalyst_agents.engine import gate as engine_gate

    facts = {"observation": {"market_comove": {"direction_relation": "OPPOSITE_DIRECTION"},
                             "sector_comove": {"direction_relation": "OPPOSITE_DIRECTION"}}}
    item = _item("x58", publisher="One")
    cluster = _role_cluster(
        attribution_item=item, event_item=item,
        attribution_quote="PayPal shares moved on the Pix news.",
        event_quote="PayPal announced it would integrate Pix.",
        attribution_roles=("ATTRIBUTION",), event_roles=("EVENT",),
    )
    for gate in (select_and_gate, engine_gate.select_and_gate):
        decision = gate([cluster], facts, GateConfig("desk"))
        assert decision.publish and decision.basis == "REPORTED_ATTRIBUTION"
    assert engine_gate.gate([cluster], facts).publish


def test_direction_conflict_blanks_attribution():
    from catalyst_agents.attribution.extraction import Extraction, check_direction
    ext = Extraction(explicit_attribution=True, attribution_quote="Intel stock gained in premarket trading after results.")
    assert check_direction(ext, -5.0).explicit_attribution is False
    assert check_direction(ext, 3.0).explicit_attribution is True
