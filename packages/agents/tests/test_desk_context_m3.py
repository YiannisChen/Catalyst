import sqlite3
from datetime import date, timedelta

from catalyst_agents.attribution.desk_context import decompose, peers_for
from catalyst_agents.attribution.extraction import (
    AboutTicker,
    EventCluster,
    EventTiming,
    EventType,
    GateConfig,
    select_and_gate,
)


def _market(tmp_path, *, beta=2.0, session_ref=2.0, session_stock=4.1, idio_today=False):
    db = tmp_path / "m.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT)")
    day, ref, stock, peer = date(2026, 1, 1), 100.0, 100.0, 100.0
    rows = []
    for i in range(140):
        r = ((i * 37) % 11 - 5) / 10.0  # deterministic pseudo-returns in %
        noise = ((i * 53) % 7 - 3) / 50.0
        ref *= 1 + r / 100
        stock *= 1 + (beta * r + noise) / 100
        peer *= 1 + (r + noise) / 100
        d = (day + timedelta(days=i)).isoformat()
        rows += [("XLF", d, ref), ("BAC", d, stock), ("SPY", d, 100 + i * 0.01), ("JPM", d, peer)]
    session = (day + timedelta(days=140)).isoformat()
    ref_today = ref * (1 + session_ref / 100)
    stock_today = stock * (1 + (session_stock if not idio_today else -session_stock) / 100)
    rows += [("XLF", session, ref_today), ("BAC", session, stock_today), ("SPY", session, 100 + 140 * 0.01),
             ("JPM", session, peer)]
    conn.executemany("INSERT INTO ohlcv (symbol, date, close) VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()
    return db, session


def test_beta_decomposition_marks_a_high_beta_sector_move_group_driven(tmp_path):
    db, session = _market(tmp_path)
    result = decompose(db, "BAC", session, sector_ticker="XLF", commodity_ticker=None)
    assert result.verdict == "GROUP_DRIVEN"
    assert result.best.kind == "SECTOR" and 1.8 < result.best.beta < 2.2
    assert "[Catalyst analysis, inferred]" in result.sentence()


def test_move_against_the_sector_is_idiosyncratic(tmp_path):
    db, session = _market(tmp_path, idio_today=True)
    assert decompose(db, "BAC", session, sector_ticker="XLF", commodity_ticker=None).verdict == "IDIOSYNCRATIC"


def test_session_close_is_not_used_for_beta(tmp_path):
    db, session = _market(tmp_path, session_stock=30.0)
    fit = decompose(db, "BAC", session, sector_ticker="XLF", commodity_ticker=None).fits[1]
    assert fit.kind == "SECTOR" and 1.8 < fit.beta < 2.2  # an extreme session return does not move beta


def test_peers_for_prefers_direct_peers_then_gics_subindustry():
    peer_map = {"targets": {"AMD": {"direct_peers": ["INTC", "NVDA"]}},
                "gics": {"tickers": {"T": ["Communication Services", "Telecom"], "VZ": ["Communication Services", "Telecom"],
                                     "META": ["Communication Services", "Interactive Media"]}}}
    assert peers_for("AMD", peer_map) == ["INTC", "NVDA"]
    assert peers_for("T", peer_map) == ["VZ"]


def _cluster(event_type, scope, explicit=0):
    item = {"evidence_id": f"{event_type}-{scope}", "eligible_at": "2026-04-08T14:00:00Z", "publisher": "P"}
    return EventCluster(event_type, ((item, None),), 1, explicit, False, scope,
                        (item, "X rose after Y.") if explicit else None, (item, "Y happened today."),
                        True, frozenset({EventTiming.INTRADAY}))


class _Decomp:
    def __init__(self, verdict, kind="SECTOR"):
        self.verdict = verdict
        self.best = type("B", (), {"kind": kind})()


def test_desk_m3_infers_group_move_and_leads_with_group_when_beta_explains_it():
    facts = {"observation": {"market_comove": {"direction_relation": "SAME_DIRECTION", "band": "DIVERGENT"}},
             "decomposition": _Decomp("GROUP_DRIVEN", "SECTOR")}
    sector_news = _cluster(EventType.SECTOR, AboutTicker.GROUP)
    analyst = _cluster(EventType.ANALYST_ACTION, AboutTicker.ISSUER, explicit=1)
    decision = select_and_gate([analyst, sector_news], facts, GateConfig("desk_m3"))
    assert decision.basis == "GROUP_MOVE_INFERRED" and decision.primary is sector_news
    # The same evidence under the frozen desk gate keeps the analyst note and never infers the group move.
    assert select_and_gate([analyst, sector_news], facts, GateConfig("desk")).primary is analyst


def test_desk_m3_needs_group_driven_verdict_and_a_matching_event_kind():
    sector_news = _cluster(EventType.SECTOR, AboutTicker.GROUP)
    mixed = {"observation": {}, "decomposition": _Decomp("MIXED")}
    assert not select_and_gate([sector_news], mixed, GateConfig("desk_m3")).publish
    commodity_driven = {"observation": {}, "decomposition": _Decomp("GROUP_DRIVEN", "COMMODITY")}
    assert not select_and_gate([sector_news], commodity_driven, GateConfig("desk_m3")).publish
    oil = _cluster(EventType.COMMODITY, AboutTicker.GROUP)
    assert select_and_gate([oil], commodity_driven, GateConfig("desk_m3")).basis == "GROUP_MOVE_INFERRED"
