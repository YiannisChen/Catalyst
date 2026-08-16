"""AMEND-5 regression tests: production lexical attribution query policy.

These tests pin the general deterministic policy for realistic attribution
queries (ticker + signed percentage + ISO date + stopwords) so the lexical arm
can never silently return zero because every natural-language token is ANDed
into one chunk. Ticker/date/percentage constraints remain structured filters.
"""
from __future__ import annotations

import pytest

from retrieval_fixtures import MANIFEST_A, build_fts5, fresh_v10_db, insert_corpus_chunk

from catalyst_data.retrieval.fts5 import retrieve_lexical
from catalyst_data.retrieval.query_policy import content_terms, fts_quote

CUTOFF = "2026-01-15T21:00:00Z"


def _seed(db, chunk_id, content_text, *, ticker=("AAPL",), available_at="2026-01-01T09:00:00Z", **kwargs):
    insert_corpus_chunk(
        db, chunk_id=chunk_id, content_text=content_text,
        ticker_associations=ticker, available_at=available_at, **kwargs,
    )
    db.commit()


# ── RED: realistic attribution queries must not silently return zero ─────────

def test_realistic_attribution_query_returns_nonempty_candidates():
    db = fresh_v10_db()
    _seed(db, "poly:move:news_v2:body:0001", "Tesla shares moved sharply after the earnings report.", ticker=("TSLA",))
    _seed(db, "poly:other:news_v2:body:0001", "Unrelated market commentary about rates.", ticker=("TSLA",))
    build_fts5(db)

    result = retrieve_lexical(
        db, query="Why did TSLA move -8.2% on 2025-07-24?",
        ticker="TSLA", cutoff=CUTOFF, requested_manifest_id=MANIFEST_A,
    )
    assert len(result.results) > 0
    assert result.candidate_count > 0
    ids = {r.chunk_id for r in result.results}
    assert "poly:move:news_v2:body:0001" in ids


def test_ticker_is_structured_filter_not_mandatory_fts_term():
    db = fresh_v10_db()
    _seed(db, "poly:tsla:news_v2:body:0001", "Shares on the move lower after the report.", ticker=("TSLA",))
    _seed(db, "poly:msft:news_v2:body:0001", "Shares on the move lower after the report.", ticker=("MSFT",))
    build_fts5(db)

    result = retrieve_lexical(
        db, query="Why did TSLA move -8.2% on 2025-07-24?",
        ticker="TSLA", cutoff=CUTOFF, requested_manifest_id=MANIFEST_A,
    )
    ids = {r.chunk_id for r in result.results}
    assert "poly:tsla:news_v2:body:0001" in ids
    assert "poly:msft:news_v2:body:0001" not in ids


def test_stopwords_and_structured_tokens_are_not_mandatory():
    db = fresh_v10_db()
    # The chunk contains only the content word "move" (no why/did/8/2/2025/07/24).
    _seed(db, "poly:bare:news_v2:body:0001", "Tesla stock is on the move after results.", ticker=("TSLA",))
    build_fts5(db)

    result = retrieve_lexical(
        db, query="Why did TSLA move -8.2% on 2025-07-24?",
        ticker="TSLA", cutoff=CUTOFF, requested_manifest_id=MANIFEST_A,
    )
    assert {r.chunk_id for r in result.results} == {"poly:bare:news_v2:body:0001"}


def test_content_terms_remove_stopwords_ticker_numbers_and_months():
    terms = content_terms("Why did TSLA move -8.2% on 2025-07-24?", "TSLA")
    assert "why" not in terms
    assert "did" not in terms
    assert "on" not in terms
    assert "8" not in terms
    assert "2025" not in terms
    assert "move" in terms
    # issuer enrichment from universe legal_name is applied for content policy
    assert "tesla" in terms


def test_universe_covers_ratified_tickers_beyond_t4_subset():
    """Production policy must recognize the full ratified universe, not a T4 subset."""
    from catalyst_data.manifests.universe import RATIFIED_TICKERS
    from catalyst_data.retrieval.query_policy import KNOWN_TICKERS, ISSUER_NAMES

    assert frozenset(RATIFIED_TICKERS) == KNOWN_TICKERS
    for ticker in ("INTC", "ADBE", "F", "C", "TSM"):
        assert ticker in KNOWN_TICKERS
        assert ticker in ISSUER_NAMES
        assert ISSUER_NAMES[ticker]  # non-empty enrichment from legal_name


def test_structured_ticker_always_stripped_even_outside_known_map():
    """Any structured ticker argument is recognized and removed from content terms."""
    from catalyst_data.retrieval.query_policy import plan_lexical_query

    plan = plan_lexical_query("Why did ZZTOP move on 2025-07-24?", "ZZTOP")
    assert "zztop" not in plan.content_terms
    assert plan.structured_ticker == "ZZTOP"


def test_issuer_name_query_strips_universe_alias():
    from catalyst_data.retrieval.query_policy import plan_lexical_query

    for ticker, name in (
        ("INTC", "Intel"), ("ADBE", "Adobe"), ("F", "Ford"),
        ("C", "Citigroup"), ("TSM", "Taiwan"),
    ):
        plan = plan_lexical_query(f"Why did {name} move on 2025-07-24?", ticker)
        assert name.casefold() not in plan.content_terms
        assert "move" in plan.content_terms or "move" in plan.terms


def test_ticker_only_and_date_only_use_temporal_window_policy():
    from catalyst_data.retrieval.query_policy import plan_lexical_query

    ticker_only = plan_lexical_query("TSLA", "TSLA")
    assert ticker_only.policy == "temporal_window"
    assert ticker_only.low_information is True
    date_only = plan_lexical_query("TSLA 2025-07-24", "TSLA")
    assert date_only.policy == "temporal_window"
    assert date_only.target_date == "2025-07-24"


def test_ticker_issuer_mismatch_does_not_inject_wrong_issuer_enrichment():
    from catalyst_data.retrieval.query_policy import plan_lexical_query

    # Query names Apple while structured filter is MSFT — enrichment is MSFT-only.
    plan = plan_lexical_query("Why did Apple move on 2025-07-24?", "MSFT")
    assert "microsoft" in plan.issuer_enrichment or "msft" in plan.issuer_enrichment
    assert "apple" not in plan.issuer_enrichment
    # Apple is a known issuer alias and is structured-stripped from content.
    assert "apple" not in plan.content_terms


def test_policy_never_injects_oracle_fields():
    from catalyst_data.retrieval import query_policy as qp
    import inspect

    source = inspect.getsource(qp)
    assert "g006" not in source
    assert "g013" not in source


def test_lexical_hot_path_uses_limit_and_count_not_unbounded_fetchall():
    """Production FTS path must push LIMIT/COUNT into SQL (no full-row fetchall)."""
    import inspect
    from catalyst_data.retrieval import fts5 as fts5_mod

    source = inspect.getsource(fts5_mod)
    assert "LIMIT ?" in source
    assert "COUNT(*)" in source
    # The unbounded pre-AMEND-5.1 fetchall of AND/OR match sets is gone.
    assert "rows = conn.execute(\n                    select_sql, [and_query" not in source


def test_deterministic_output():
    db = fresh_v10_db()
    _seed(db, "poly:a:news_v2:body:0001", "Tesla earnings report showed strong results.", ticker=("TSLA",))
    _seed(db, "poly:b:news_v2:body:0001", "Tesla shares moved on guidance.", ticker=("TSLA",))
    build_fts5(db)

    kwargs = dict(
        query="Why did TSLA move -8.2% on 2025-07-24?",
        ticker="TSLA", cutoff=CUTOFF, requested_manifest_id=MANIFEST_A,
    )
    first = retrieve_lexical(db, **kwargs)
    second = retrieve_lexical(db, **kwargs)
    assert [r.chunk_id for r in first.results] == [r.chunk_id for r in second.results]


def test_fts_quote_is_safe():
    assert fts_quote("move") == '"move"'
    with pytest.raises(ValueError):
        fts_quote('evil"term')


def test_low_information_query_uses_temporal_window_policy():
    db = fresh_v10_db()
    # Query contains only structured tokens (ticker + date); temporal-window
    # ranking must surface near-session eligible chunks without event-term OR.
    _seed(
        db, "poly:filing:news_v2:body:0001",
        "Tesla filed its earnings report with results.",
        ticker=("TSLA",), available_at="2025-07-24T12:00:00Z",
    )
    _seed(
        db, "poly:old:news_v2:body:0001",
        "Ancient unrelated history.",
        ticker=("TSLA",), available_at="2025-01-02T12:00:00Z",
    )
    build_fts5(db)

    result = retrieve_lexical(
        db, query="TSLA 2025-07-24",
        ticker="TSLA", cutoff=CUTOFF, requested_manifest_id=MANIFEST_A,
        include_trace=True,
    )
    assert len(result.results) > 0
    assert result.trace is not None
    assert result.trace.policy == "temporal_window"
    assert result.trace.match_mode == "temporal"
    ids = [r.chunk_id for r in result.results]
    assert ids[0] == "poly:filing:news_v2:body:0001"


def test_temporal_ranking_prefers_near_session_evidence():
    db = fresh_v10_db()
    _seed(db, "poly:far:news_v2:body:0001", "Tesla shares moved after results.",
          ticker=("TSLA",), available_at="2025-01-10T12:00:00Z")
    _seed(db, "poly:near:news_v2:body:0001", "Tesla shares moved after results.",
          ticker=("TSLA",), available_at="2025-07-24T12:00:00Z")
    build_fts5(db)

    result = retrieve_lexical(
        db, query="Why did TSLA move -8.2% on 2025-07-24?",
        ticker="TSLA", cutoff=CUTOFF, requested_manifest_id=MANIFEST_A,
        candidate_depth=20, top_k=8, include_trace=True,
    )
    assert result.results[0].chunk_id == "poly:near:news_v2:body:0001"
    assert result.trace is not None
    assert result.trace.match_mode in {"AND", "OR"}
    # matched_count must not materialize unbounded full-history OR
    assert result.trace.matched_row_count <= result.trace.eligible_row_count


def test_empty_raw_query_keeps_documented_empty_query_policy():
    db = fresh_v10_db()
    _seed(db, "poly:a:news_v2:body:0001", "Tesla earnings report.", ticker=("TSLA",))
    build_fts5(db)

    result = retrieve_lexical(
        db, query="   ", ticker="TSLA", cutoff=CUTOFF,
        requested_manifest_id=MANIFEST_A,
    )
    assert len(result.results) == 0
    assert result.fallback_reason == "empty_query"


def test_no_lookahead_beyond_cutoff_with_realistic_query():
    db = fresh_v10_db()
    _seed(db, "poly:pre:news_v2:body:0001", "Tesla earnings report results.", ticker=("TSLA",),
          available_at="2026-01-01T09:00:00Z")
    _seed(db, "poly:post:news_v2:body:0001", "Tesla earnings report results.", ticker=("TSLA",),
          available_at="2026-01-16T09:00:00Z")
    build_fts5(db)

    result = retrieve_lexical(
        db, query="Why did TSLA move -8.2% on 2025-07-24?",
        ticker="TSLA", cutoff=CUTOFF, requested_manifest_id=MANIFEST_A,
    )
    ids = {r.chunk_id for r in result.results}
    assert "poly:post:news_v2:body:0001" not in ids
    assert "poly:pre:news_v2:body:0001" in ids


def test_sql_fallback_uses_same_content_term_policy():
    db = fresh_v10_db()
    _seed(db, "poly:move:news_v2:body:0001", "Tesla shares moved after the earnings report.", ticker=("TSLA",))
    _seed(db, "poly:noise:news_v2:body:0001", "Completely unrelated content about weather.", ticker=("TSLA",))
    # No FTS table: sql_like degraded fallback must still match on content terms.

    result = retrieve_lexical(
        db, query="Why did TSLA move -8.2% on 2025-07-24?",
        ticker="TSLA", cutoff=CUTOFF, requested_manifest_id=MANIFEST_A,
    )
    assert result.is_degraded
    assert result.mode_served == "sql_like"
    assert len(result.results) > 0
    assert "poly:move:news_v2:body:0001" in {r.chunk_id for r in result.results}


# ── AMEND-5.2: temporal identity (structured center, never query override) ────

def test_resolve_temporal_center_uses_structured_session_not_query_date():
    """Structured trade/session/cutoff date is the retrieval center; query date never wins."""
    from catalyst_data.retrieval.query_policy import resolve_temporal_center

    resolved = resolve_temporal_center(
        query="Why did TSLA move on 2025-07-24?",
        cutoff="2026-01-15T21:00:00Z",
        session_date="2026-01-15",
    )
    assert resolved.center_date == "2026-01-15"
    assert resolved.query_date == "2025-07-24"
    assert resolved.conflict is True
    assert resolved.decision == "structured_ignore_query"


def test_resolve_temporal_center_prefers_session_over_cutoff_and_trade_date():
    from catalyst_data.retrieval.query_policy import resolve_temporal_center

    resolved = resolve_temporal_center(
        query="move on 2025-01-01",
        cutoff="2026-01-15T21:00:00Z",
        trade_date="2026-01-14",
        session_date="2026-01-13",
    )
    assert resolved.center_date == "2026-01-13"
    assert resolved.conflict is True


def test_resolve_temporal_center_no_conflict_when_query_matches():
    from catalyst_data.retrieval.query_policy import resolve_temporal_center

    resolved = resolve_temporal_center(
        query="Why did TSLA move on 2025-07-24?",
        cutoff="2025-07-24T20:00:00Z",
        session_date="2025-07-24",
    )
    assert resolved.center_date == "2025-07-24"
    assert resolved.query_date == "2025-07-24"
    assert resolved.conflict is False
    assert resolved.decision == "structured"


def test_fts5_conflicting_query_date_does_not_override_structured_center():
    """FTS5 temporal ranking must center on cutoff session, not conflicting query date."""
    db = fresh_v10_db()
    # Near structured center (2026-01-15 via CUTOFF)
    _seed(
        db, "poly:near-cutoff:news_v2:body:0001",
        "Tesla shares moved after the earnings report.",
        ticker=("TSLA",), available_at="2026-01-14T15:00:00Z",
    )
    # Near the *query* date (2025-07-24) — must not win the temporal center
    _seed(
        db, "poly:near-query:news_v2:body:0001",
        "Tesla shares moved after the earnings report.",
        ticker=("TSLA",), available_at="2025-07-24T15:00:00Z",
    )
    build_fts5(db)

    result = retrieve_lexical(
        db,
        query="Why did TSLA move -8.2% on 2025-07-24?",
        ticker="TSLA",
        cutoff=CUTOFF,  # 2026-01-15T21:00:00Z
        requested_manifest_id=MANIFEST_A,
        include_trace=True,
        top_k=2,
        candidate_depth=10,
    )
    assert result.results, "expected non-empty results"
    assert result.results[0].chunk_id == "poly:near-cutoff:news_v2:body:0001"
    assert result.trace is not None
    assert result.trace.temporal_center_date == "2026-01-15"
    assert result.trace.query_date_conflict is True
    assert result.trace.query_date == "2025-07-24"


def test_sql_fallback_conflicting_query_date_uses_structured_center():
    db = fresh_v10_db()
    _seed(
        db, "poly:near-cutoff:news_v2:body:0001",
        "Tesla shares moved after the earnings report.",
        ticker=("TSLA",), available_at="2026-01-14T15:00:00Z",
    )
    _seed(
        db, "poly:near-query:news_v2:body:0001",
        "Tesla shares moved after the earnings report.",
        ticker=("TSLA",), available_at="2025-07-24T15:00:00Z",
    )
    # No FTS build → sql_like

    result = retrieve_lexical(
        db,
        query="Why did TSLA move -8.2% on 2025-07-24?",
        ticker="TSLA",
        cutoff=CUTOFF,
        requested_manifest_id=MANIFEST_A,
        include_trace=True,
        top_k=2,
        candidate_depth=10,
    )
    assert result.mode_served == "sql_like"
    assert result.results[0].chunk_id == "poly:near-cutoff:news_v2:body:0001"
    assert result.trace is not None
    assert result.trace.temporal_center_date == "2026-01-15"
    assert result.trace.query_date_conflict is True


def test_audit_parity_conflicting_query_date_shares_structured_center():
    """Fast audit and production retriever audit must agree on structured center."""
    from catalyst_eval.post_import.lexical_audit import (
        _fast_lexical_audit,
        _production_retriever_audit,
    )

    db = fresh_v10_db()
    _seed(
        db, "poly:near-cutoff:news_v2:body:0001",
        "Tesla shares moved after the earnings report.",
        ticker=("TSLA",), available_at="2026-01-14T15:00:00Z",
    )
    _seed(
        db, "poly:near-query:news_v2:body:0001",
        "Tesla shares moved after the earnings report.",
        ticker=("TSLA",), available_at="2025-07-24T15:00:00Z",
    )
    build_fts5(db)

    kwargs = dict(
        case_id="conflict-case",
        ticker="TSLA",
        cutoff=CUTOFF,
        query="Why did TSLA move -8.2% on 2025-07-24?",
        manifest_id=MANIFEST_A,
        session_date="2026-01-15",
        candidate_depth=10,
        should_refuse=False,
    )
    # build_id None → served corpus_chunks_fts
    fast = _fast_lexical_audit(db, build_id=None, **kwargs)
    prod = _production_retriever_audit(db, **kwargs)
    assert fast.temporal_center_date == "2026-01-15"
    assert prod.temporal_center_date == "2026-01-15"
    assert fast.query_date_conflict is True
    assert prod.query_date_conflict is True
    assert fast.top_chunks[0].chunk_id == prod.top_chunks[0].chunk_id
    assert fast.top_chunks[0].chunk_id == "poly:near-cutoff:news_v2:body:0001"


# ── AMEND-5.2C P2: provenance-aware ticker/issuer claims ─────────────────────

def test_query_ticker_claim_is_frozen_typed_record():
    from catalyst_data.retrieval.query_policy import QueryTickerClaim

    claim = QueryTickerClaim(
        ticker="TSLA", source="explicit_symbol",
        raw_text="TSLA", start=8, end=12,
    )
    assert claim.ticker == "TSLA"
    assert claim.source == "explicit_symbol"
    assert claim.raw_text == "TSLA"
    assert claim.start == 8
    assert claim.end == 12
    with pytest.raises(Exception):
        claim.ticker = "AAPL"  # frozen


def test_collect_query_claims_returns_typed_claims_with_provenance():
    from catalyst_data.retrieval.query_policy import (
        QueryTickerClaim,
        collect_query_claims,
    )

    text = "Apple and Intel both cut guidance after TSLA earnings"
    claims = collect_query_claims(text, None)
    assert claims
    assert all(isinstance(c, QueryTickerClaim) for c in claims)
    by_ticker = {c.ticker: c for c in claims}
    assert by_ticker["AAPL"].source == "issuer_brand"
    assert by_ticker["INTC"].source == "issuer_brand"
    assert by_ticker["TSLA"].source == "explicit_symbol"
    for claim in claims:
        assert text[claim.start:claim.end] == claim.raw_text


def test_marked_symbol_claims_are_recognized():
    from catalyst_data.retrieval.query_policy import collect_query_claims

    assert {
        (c.ticker, c.source)
        for c in collect_query_claims("Why did $TSLA move after NYSE:F?", None)
    } == {("TSLA", "marked_symbol"), ("F", "marked_symbol")}
    assert {
        (c.ticker, c.source)
        for c in collect_query_claims("Why did NASDAQ:TSLA move?", None)
    } == {("TSLA", "marked_symbol")}


def test_single_letter_claims_are_always_ambiguous():
    from catalyst_data.retrieval.query_policy import (
        claim_tickers,
        collect_query_claims,
        decide_claim_consistency,
    )

    for query in ("Why did C move on 2025-07-24?", "Why did F move on 2025-07-24?"):
        claims = collect_query_claims(query, None)
        assert len(claims) == 1
        assert claims[0].source == "single_letter_symbol"
        assert decide_claim_consistency(claims, "AAPL", query=query) is None, query


def test_lowercase_intel_is_brand_claim_but_fails_open():
    from catalyst_data.retrieval.query_policy import (
        collect_query_claims,
        decide_claim_consistency,
    )

    claims = collect_query_claims("new intel guidance", None)
    assert {c.ticker for c in claims} == {"INTC"}
    assert claims[0].source == "issuer_brand"
    assert decide_claim_consistency(claims, "AAPL", query="new intel guidance") is None


def test_claim_decision_hard_mismatch_only_for_strong_unique_symbols():
    from catalyst_data.retrieval.query_policy import (
        collect_query_claims,
        decide_claim_consistency,
    )

    hard = (
        ("Why did TSLA move?", "AAPL"),
        ("Why did $TSLA move?", "AAPL"),
        ("Why did NASDAQ:TSLA move?", "AAPL"),
        ("Why did $F move?", "AAPL"),
        ("Why did NYSE:F move?", "AAPL"),
    )
    for query, structured in hard:
        claims = collect_query_claims(query, None)
        assert decide_claim_consistency(claims, structured, query=query) is False, query

    consistent = (
        ("Why did Apple fall after TSLA earnings?", "AAPL"),
        ("Did Nvidia guidance hurt AMD?", "AMD"),
    )
    for query, structured in consistent:
        claims = collect_query_claims(query, None)
        assert decide_claim_consistency(claims, structured, query=query) is True, query

    soft = (
        ("Did Intel supply issues matter?", "AAPL"),
        ("new intel", "AAPL"),
        ("Form F-1", "AAPL"),
        ("vitamin C", "AAPL"),
        ("section C", "AAPL"),
        ("plain C", "AAPL"),
        ("plain F", "AAPL"),
        ("Microsoft and Nvidia both missed", "AAPL"),
        ("", "AAPL"),
        ("Taiwan tensions rose overnight", "AAPL"),
        ("Taiwan Semiconductor", "AAPL"),
        ("APPL dropped about 3% on June 12, 2025", "AAPL"),
        ("Why did C move on 2025-07-24?", "AAPL"),
        ("Why did F move on 2025-07-24?", "AAPL"),
    )
    for query, structured in soft:
        claims = collect_query_claims(query, None)
        assert decide_claim_consistency(claims, structured, query=query) is None, query


def test_claim_tickers_helper_dedupes_without_losing_provenance():
    from catalyst_data.retrieval.query_policy import (
        QueryTickerClaim,
        claim_tickers,
        collect_query_claims,
    )

    raw = collect_query_claims("TSLA after $TSLA", None)
    assert len(raw) == 2
    assert claim_tickers(raw) == frozenset({"TSLA"})
    assert all(isinstance(c, QueryTickerClaim) for c in raw)


# ── AMEND-5.2C follow-up P1: direct-target intent policy ─────────────────────

def test_direct_target_claim_classification():
    from catalyst_data.retrieval.query_policy import (
        collect_query_claims,
        identify_direct_target_claims,
    )

    direct = (
        "Why did TSLA move?",
        "Why did $TSLA fall?",
        "What caused TSLA to rise?",
        "Why was TSLA down 8%?",
        "TSLA fell 8% today — why?",
        "What drove TSLA's move?",
    )
    for query in direct:
        claims = collect_query_claims(query, None)
        assert {c.ticker for c in identify_direct_target_claims(query, claims)} == {"TSLA"}, query

    context = (
        "Did $TSLA price cuts hurt the company?",
        "Did TSLA price cuts hurt AAPL?",
        "Was NVDA guidance responsible for the selected company's decline?",
        "Compare AAPL with TSLA.",
        "Apple fell after TSLA earnings.",
        "Microsoft and Nvidia both missed.",
    )
    for query in context:
        claims = collect_query_claims(query, None)
        # In these queries no claim is the direct price-move subject; AAPL may
        # be the impact target, TSLA/NVDA stay context.
        assert set(identify_direct_target_claims(query, claims)) == set(), (query, claims)


def test_impact_target_classification():
    from catalyst_data.retrieval.query_policy import (
        collect_query_claims,
        identify_impact_target_claims,
    )

    claims = collect_query_claims("Did Nvidia guidance hurt AMD?", None)
    assert {c.ticker for c in identify_impact_target_claims("Did Nvidia guidance hurt AMD?", claims)} == {"AMD"}

    claims = collect_query_claims("Did TSLA price cuts hurt the company?", None)
    assert identify_impact_target_claims("Did TSLA price cuts hurt the company?", claims) == ()

    claims = collect_query_claims("Did TSLA price cuts hurt AAPL?", None)
    assert {c.ticker for c in identify_impact_target_claims("Did TSLA price cuts hurt AAPL?", claims)} == {"AAPL"}


def test_followup_decision_competitor_context_not_mismatch():
    from catalyst_data.retrieval.query_policy import (
        collect_query_claims,
        decide_claim_consistency,
    )

    query = "Did $TSLA price cuts hurt the company?"
    claims = collect_query_claims(query, None)
    assert decide_claim_consistency(claims, "AAPL", query=query) is None


def test_followup_decision_structured_context_not_consistent():
    """AAPL appearing as context must not make the query consistent when TSLA
    is the direct movement subject."""
    from catalyst_data.retrieval.query_policy import (
        collect_query_claims,
        decide_claim_consistency,
        direct_target_mismatch_symbol,
    )

    query = "Why did TSLA move after AAPL earnings?"
    claims = collect_query_claims(query, None)
    assert decide_claim_consistency(claims, "AAPL", query=query) is False
    assert direct_target_mismatch_symbol(claims, "AAPL", query=query) == "TSLA"


def test_followup_decision_impact_target_structured_consistent():
    from catalyst_data.retrieval.query_policy import (
        collect_query_claims,
        decide_claim_consistency,
    )

    query = "Did Nvidia guidance hurt AMD?"
    claims = collect_query_claims(query, None)
    assert decide_claim_consistency(claims, "AMD", query=query) is True

    query = "Did TSLA price cuts hurt AAPL?"
    claims = collect_query_claims(query, None)
    assert decide_claim_consistency(claims, "AAPL", query=query) is True


def test_followup_foreign_impact_object_fails_open():
    """A foreign symbol as the impacted object is an impact relationship and
    must fail open — never a hard mismatch."""
    from catalyst_data.retrieval.query_policy import (
        collect_query_claims,
        decide_claim_consistency,
    )

    query = "Did Nvidia guidance hurt AMD?"
    claims = collect_query_claims(query, None)
    assert decide_claim_consistency(claims, "AAPL", query=query) is None
