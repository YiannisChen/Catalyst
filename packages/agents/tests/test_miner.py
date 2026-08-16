from __future__ import annotations

import pytest

from attribution_fixtures import FixtureRetriever, RecordingCutoffPolicy
from catalyst_agents.nodes.miner import _build_query, _check_magnitude_plausible, _extract_query_ticker, miner
from catalyst_agents.state import OutputStatus


def _state(query=None):
    return {"ticker": "AAPL", "trade_date": "2026-01-15", "query": query, "price_move_pct": None, "corpus_manifest_id": "corpus-fixture-v1"}


def test_build_query_with_nlp_query():
    assert _build_query(_state("Why did Apple drop?")) == "Why did Apple drop?"


def test_build_query_without_query():
    assert _build_query(_state()).startswith("Why did AAPL move")


def test_miner_calls_injected_retriever_with_canonical_cutoff():
    retriever = FixtureRetriever()
    cutoff_policy = RecordingCutoffPolicy()
    result = miner(_state("Why did Apple crash after earnings?"), retriever=retriever, cutoff_policy=cutoff_policy)

    assert retriever.calls == [{
        "query": "Why did Apple crash after earnings?",
        "ticker": "AAPL",
        "cutoff": "2026-01-15T21:00:00Z",
        "requested_manifest_id": "corpus-fixture-v1",
        "top_k": 8,
        "candidate_depth": 20,
    }]
    assert cutoff_policy.calls == [("AAPL", "2026-01-15", "attribution")]
    assert len(result["reranked_chunks"]) == 2


def test_miner_projects_all_retrieved_evidence_fields():
    result = miner(_state(), retriever=FixtureRetriever(), cutoff_policy=RecordingCutoffPolicy())
    chunk = result["reranked_chunks"][0]
    for key in {
        "asset_id", "chunk_id", "document_id", "content_text", "available_at",
        "source_class", "ticker_associations", "dedup_cluster_id",
        "cluster_first_available_at", "representative_document_id", "is_novel",
        "lexical_raw_score", "lexical_rank", "corpus_manifest_id",
        "index_manifest_id", "mode_requested", "mode_served", "is_degraded",
        "fallback_reason",
    }:
        assert key in chunk


def test_miner_retriever_exception_becomes_system_error():
    result = miner(_state(), retriever=FixtureRetriever(fail=True), cutoff_policy=RecordingCutoffPolicy())
    assert result["output_status"] == OutputStatus.SYSTEM_ERROR
    assert result["validation_error"] == "retriever_error"
    assert result["retrieved_chunks"] == []


def test_miner_missing_dependencies_are_typed_system_error():
    result = miner(_state())
    assert result["output_status"] == OutputStatus.SYSTEM_ERROR
    assert result["validation_error"] == "missing_retrieval_dependency"


def test_miner_sets_ticker_consistent_false_on_query_ticker_mismatch():
    out = miner(_state("TSLA dropped about 3% on June 12, 2025. Why?"), retriever=FixtureRetriever(), cutoff_policy=RecordingCutoffPolicy())
    assert out["query_ticker_raw"] == "TSLA"
    assert out["ticker_consistent"] is False
    assert out["retrieved_chunks"] == []


def test_extract_query_ticker_uses_known_ticker_not_acronym():
    assert _extract_query_ticker("On March 18, 2025, Microsoft CEO resigned unexpectedly", {"AAPL", "MSFT", "TSLA", "MRNA"}) == "MSFT"


def test_extract_query_ticker_ignores_fda_and_keeps_symbol():
    assert _extract_query_ticker("FDA approved MRNA vaccine update", {"MRNA", "AAPL"}) == "MRNA"


def test_check_magnitude_plausible_extreme():
    assert _check_magnitude_plausible(actual_pct=1.5, claimed_pct=34.0, tolerance=10.0) is False


def test_build_query_empty_string_uses_generated_query():
    assert _build_query(_state("")) == "Why did AAPL move on 2026-01-15?"


def test_miner_skips_retrieval_when_context_session_is_invalid():
    retriever = FixtureRetriever()
    state = {**_state(), "market_session_valid": False}

    result = miner(state, retriever=retriever, cutoff_policy=RecordingCutoffPolicy())

    assert retriever.calls == []
    assert result["retrieved_chunks"] == []
    assert result["reranked_chunks"] == []
    assert result["output_status"] == OutputStatus.ABSTAIN


def test_miner_expansion_delegates_to_injected_retriever():
    retriever = FixtureRetriever()
    state = {**_state(), "current_layer": "macro"}

    result = miner(state, retriever=retriever, cutoff_policy=RecordingCutoffPolicy())

    assert result.get("error_type") is None
    assert retriever.calls[0]["query"].endswith("macro market sector rates policy context")
    assert result["retrieved_chunks"]


def test_miner_emits_reranker_provenance_fields():
    """rerank_score/reranker_score/reranker_rank must reach miner chunk dicts
    so exported reranked_chunks evidence is auditable."""
    from catalyst_agents.attribution.provider import RetrievedEvidence
    from catalyst_agents.nodes.miner import _evidence_to_chunk

    item = RetrievedEvidence(
        chunk_id="c1", document_id="d1", content_text="evidence",
        available_at="2026-01-15T18:00:00Z", source_class="issuer_disclosure",
        ticker_associations=("AAPL",), dedup_cluster_id=None,
        cluster_first_available_at="2026-01-15T18:00:00Z",
        representative_document_id="d1", is_novel=False,
        lexical_raw_score=-2.0, lexical_rank=3,
        corpus_manifest_id="corpus-fixture-v1", index_manifest_id="index-fixture-v1",
        mode_requested="reranked", mode_served="reranked", is_degraded=False,
        fallback_reason=None, fusion_score=0.05,
        reranker_score=0.97, reranker_rank=1,
    )
    chunk = _evidence_to_chunk(item)
    assert chunk["rerank_score"] == 0.97
    assert chunk["reranker_score"] == 0.97
    assert chunk["reranker_rank"] == 1
    assert chunk["rank"] == 1
    assert chunk["score"] == 0.97


# ── AMEND-5.2: full-universe ticker recognition + mismatch short-circuit ─────

def test_miner_extract_recognizes_full_universe_tickers():
    """Miner must recognize ratified universe tickers (not a hardcoded subset)."""
    from catalyst_data.manifests.universe import RATIFIED_TICKERS
    from catalyst_data.retrieval.query_policy import KNOWN_TICKERS, TICKER_ALIASES
    from catalyst_agents.nodes.miner import _extract_query_ticker

    known = set(KNOWN_TICKERS)
    assert frozenset(RATIFIED_TICKERS) == KNOWN_TICKERS
    for ticker in ("TSM", "INTC", "ADBE", "F", "C"):
        assert ticker in known
        assert _extract_query_ticker(f"Why did {ticker} move on 2025-07-24?", known) == ticker


def test_miner_extract_recognizes_canonical_issuer_aliases():
    from catalyst_data.retrieval.query_policy import KNOWN_TICKERS, TICKER_ALIASES
    from catalyst_agents.nodes.miner import _extract_query_ticker

    known = set(KNOWN_TICKERS)
    # Canonical issuer aliases from universe legal-name maps.
    cases = (
        ("Intel", "INTC"),
        ("Adobe", "ADBE"),
        ("Ford", "F"),
        ("Citigroup", "C"),
        ("Taiwan Semiconductor", "TSM"),  # phrase only — lone Taiwan is not a claim
    )
    for alias, expected in cases:
        if alias.upper() in TICKER_ALIASES or expected in known:
            got = _extract_query_ticker(f"Why did {alias} move yesterday?", known)
            assert got == expected, f"alias {alias!r} -> {got!r}, expected {expected!r}"


def test_miner_mismatch_short_circuits_with_zero_provider_and_retrieval_calls():
    """Query ticker/issuer conflicting with structured ticker must not invoke provider/retriever."""
    retriever = FixtureRetriever()
    cutoff_policy = RecordingCutoffPolicy()
    # Query claims TSLA; structured state ticker is AAPL.
    out = miner(
        _state("Why did TSLA drop 3% on June 12, 2025?"),
        retriever=retriever,
        cutoff_policy=cutoff_policy,
    )
    assert out["ticker_consistent"] is False
    assert out["query_ticker_raw"] == "TSLA"
    assert out["retrieved_chunks"] == []
    assert out["reranked_chunks"] == []
    assert out.get("provider_calls", 0) == 0
    assert out.get("retrieval_calls", 0) == 0
    assert retriever.calls == []  # no retrieval invocation
    assert cutoff_policy.calls == []  # no cutoff/provider path


def test_miner_single_issuer_brand_fails_open_not_short_circuit():
    """A lone issuer brand (Tesla) cannot prove the structured ticker wrong:
    it is a peer/competitor mention, so retrieval must proceed (fail-open)."""
    retriever = FixtureRetriever()
    cutoff_policy = RecordingCutoffPolicy()
    out = miner(
        _state("Why did Tesla move after earnings?"),
        retriever=retriever,
        cutoff_policy=cutoff_policy,
    )
    assert out["ticker_consistent"] is not False
    assert out["query_ticker_raw"] is None
    assert retriever.calls
    assert cutoff_policy.calls
    assert out["retrieved_chunks"]


def test_miner_does_not_hardcode_ticker_subset():
    """Production miner known-ticker source must be the ratified universe, not a literal set."""
    import inspect
    from catalyst_agents.nodes import miner as miner_mod

    source = inspect.getsource(miner_mod.miner)
    # Old hardcoded subset must not remain in miner().
    assert '"AAPL", "MSFT", "TSLA"' not in source
    assert "MRNA" not in source or "KNOWN_TICKERS" in source or "known_tickers" in source
    # Must reference shared universe/query_policy helpers.
    mod_source = inspect.getsource(miner_mod)
    assert "KNOWN_TICKERS" in mod_source or "known_tickers" in mod_source or "ticker_alias" in mod_source


# ── AMEND-5.2 skeptic: English/universe collisions + single-letter false mismatch ─

def test_extract_does_not_claim_english_collision_ticker_before_issuer_alias():
    """'now'/'cost'/'snap' as English must not beat issuer aliases (Apple→AAPL)."""
    assert _extract_query_ticker(
        "Why did Apple move now after earnings?", None
    ) == "AAPL"
    assert _extract_query_ticker(
        "Why did Apple cut cost after earnings?", None
    ) == "AAPL"
    assert _extract_query_ticker(
        "Why did Apple snap lower after guidance?", None
    ) == "AAPL"


def test_extract_does_not_claim_english_collision_before_explicit_ticker():
    """Lowercase English collision words must not override an explicit multi-char ticker."""
    assert _extract_query_ticker(
        "Why did AAPL move now after earnings?", None
    ) == "AAPL"
    assert _extract_query_ticker(
        "Why did TSLA cut cost after delivery?", None
    ) == "TSLA"


def test_extract_still_recognizes_uppercase_collision_tickers_when_claimed():
    """Uppercase COST/NOW/SNAP as ticker symbols remain recognizable."""
    assert _extract_query_ticker("Why did COST move on 2025-07-24?", None) == "COST"
    assert _extract_query_ticker("Why did NOW move on 2025-07-24?", None) == "NOW"
    assert _extract_query_ticker("Why did SNAP move on 2025-07-24?", None) == "SNAP"


def test_extract_single_letter_not_claimed_in_finance_english():
    """Series C / C-suite / F grade must not be read as tickers C or F."""
    assert _extract_query_ticker(
        "Why did Apple drop after Series C headlines?", None
    ) == "AAPL"
    assert _extract_query_ticker(
        "Why did Apple drop after C-suite changes?", None
    ) == "AAPL"
    assert _extract_query_ticker(
        "Why did ADBE get an F grade on privacy?", None
    ) == "ADBE"
    assert _extract_query_ticker(
        "Why did Adobe get an F grade on privacy?", None
    ) == "ADBE"


def test_extract_still_recognizes_isolated_single_letter_tickers():
    """Isolated C / F as the claim ticker remain valid (Citigroup / Ford)."""
    assert _extract_query_ticker("Why did C move on 2025-07-24?", None) == "C"
    assert _extract_query_ticker("Why did F move on 2025-07-24?", None) == "F"


def test_extract_respects_caller_known_set_without_forcing_universe():
    """When a narrow known set is passed, do not force-match full-universe tokens."""
    # Only AAPL/MSFT allowed — COST (universe ticker) must not win over MSFT alias.
    assert _extract_query_ticker(
        "Why did Microsoft cut cost after earnings?",
        {"AAPL", "MSFT"},
    ) == "MSFT"
    # COST not in allowed → None even if uppercase (caller restricted set).
    assert _extract_query_ticker("Why did COST move?", {"AAPL", "MSFT"}) is None


def test_miner_apple_now_query_stays_consistent_and_invokes_retriever():
    """Consistent AAPL + English 'now' must not false short-circuit retrieval."""
    retriever = FixtureRetriever()
    cutoff_policy = RecordingCutoffPolicy()
    out = miner(
        _state("Why did Apple move now after earnings?"),
        retriever=retriever,
        cutoff_policy=cutoff_policy,
    )
    assert out.get("ticker_consistent") is not False
    assert out.get("query_ticker_raw") in (None, "AAPL")
    assert retriever.calls  # retrieval invoked
    assert cutoff_policy.calls  # cutoff path invoked
    assert out["retrieved_chunks"]


def test_miner_series_c_query_stays_consistent_and_invokes_retriever():
    retriever = FixtureRetriever()
    cutoff_policy = RecordingCutoffPolicy()
    out = miner(
        _state("Why did Apple drop after Series C headlines?"),
        retriever=retriever,
        cutoff_policy=cutoff_policy,
    )
    assert out.get("ticker_consistent") is not False
    assert out.get("query_ticker_raw") in (None, "AAPL")
    assert retriever.calls
    assert cutoff_policy.calls


def test_miner_f_grade_query_with_adbe_structured_stays_consistent():
    retriever = FixtureRetriever()
    cutoff_policy = RecordingCutoffPolicy()
    state = {
        "ticker": "ADBE",
        "trade_date": "2026-01-15",
        "query": "Why did Adobe get an F grade on privacy?",
        "price_move_pct": None,
        "corpus_manifest_id": "corpus-fixture-v1",
    }
    out = miner(state, retriever=retriever, cutoff_policy=cutoff_policy)
    assert out.get("ticker_consistent") is not False
    assert out.get("query_ticker_raw") in (None, "ADBE")
    assert retriever.calls


# ── AMEND-5.2A P1: conservative symbol vs issuer (no legal-name false claims) ─

_MANAGER_FALSE_POSITIVES = (
    "move now",
    "Series C",
    "F grade",
    "cost pressure",
    "general market",
    "advanced manufacturing",
    "target price",
    "technology spending",
    "bank funding",
    "research spending",
    "platforms demand",
)


@pytest.mark.parametrize("query", _MANAGER_FALSE_POSITIVES)
def test_extract_manager_false_positive_pack_returns_none(query):
    """Ordinary English/finance phrases must not produce a claimed ticker/issuer."""
    assert _extract_query_ticker(query, None) is None


def test_extract_ordinary_word_alone_never_claims_universe_token():
    for word in ("now", "cost", "snap", "general", "advanced", "target", "platforms", "bank"):
        assert _extract_query_ticker(word, None) is None
        assert _extract_query_ticker(word.capitalize(), None) is None


def test_extract_ordinary_word_before_and_after_real_issuer():
    assert _extract_query_ticker("now Apple moved after cost pressure", None) == "AAPL"
    assert _extract_query_ticker("Apple moved after cost pressure now", None) == "AAPL"
    assert _extract_query_ticker("advanced manufacturing Intel results", None) == "INTC"
    assert _extract_query_ticker("Intel advanced manufacturing update", None) == "INTC"


def test_extract_no_issuer_in_query_returns_none():
    """Identity may come only from structured ticker — free text need not claim one."""
    assert _extract_query_ticker("Why did the stock move after earnings?", None) is None
    assert _extract_query_ticker("What caused the session move?", None) is None


def test_extract_real_symbols_and_conservative_issuers():
    assert _extract_query_ticker("Why did TSLA move on 2025-07-24?", None) == "TSLA"
    assert _extract_query_ticker("Why did TSM move?", None) == "TSM"
    assert _extract_query_ticker("Why did INTC move?", None) == "INTC"
    assert _extract_query_ticker("Why did ADBE move?", None) == "ADBE"
    assert _extract_query_ticker("Why did C move on 2025-07-24?", None) == "C"
    assert _extract_query_ticker("Why did F move on 2025-07-24?", None) == "F"
    assert _extract_query_ticker("Why did Apple move?", None) == "AAPL"
    assert _extract_query_ticker("Why did Intel move?", None) == "INTC"
    assert _extract_query_ticker("Why did Adobe move?", None) == "ADBE"
    assert _extract_query_ticker("Why did Ford move?", None) == "F"
    assert _extract_query_ticker("Why did Citigroup move?", None) == "C"
    assert _extract_query_ticker("Why did Taiwan Semiconductor move?", None) == "TSM"


def test_extract_does_not_use_legal_name_token_as_issuer_claim():
    """Unique legal-name fragments must not be auto-claims (AMEND-5.2A)."""
    # These map via TICKER_ALIASES today but must not be issuer claims.
    assert _extract_query_ticker("general market conditions", None) is None
    assert _extract_query_ticker("platforms demand improved", None) is None
    assert _extract_query_ticker("advanced devices outlook", None) is None


def test_miner_no_issuer_query_uses_structured_ticker_and_retrieves():
    retriever = FixtureRetriever()
    cutoff_policy = RecordingCutoffPolicy()
    out = miner(
        _state("Why did the stock move after earnings?"),
        retriever=retriever,
        cutoff_policy=cutoff_policy,
    )
    assert out.get("ticker_consistent") is not False
    assert out.get("query_ticker_raw") is None
    assert retriever.calls
    assert cutoff_policy.calls


def test_miner_false_positive_phrases_do_not_short_circuit():
    retriever = FixtureRetriever()
    cutoff_policy = RecordingCutoffPolicy()
    out = miner(
        _state("general market cost pressure and platforms demand"),
        retriever=retriever,
        cutoff_policy=cutoff_policy,
    )
    assert out.get("ticker_consistent") is not False
    assert out.get("provider_calls", 0) != 0 or retriever.calls
    assert retriever.calls
    assert cutoff_policy.calls


def test_miner_true_mismatch_still_short_circuits_before_cutoff_and_retriever():
    retriever = FixtureRetriever()
    cutoff_policy = RecordingCutoffPolicy()
    out = miner(
        _state("Why did TSLA drop 3% on June 12, 2025?"),
        retriever=retriever,
        cutoff_policy=cutoff_policy,
    )
    assert out["ticker_consistent"] is False
    assert out["query_ticker_raw"] == "TSLA"
    assert out.get("provider_calls", 0) == 0
    assert out.get("retrieval_calls", 0) == 0
    assert retriever.calls == []
    assert cutoff_policy.calls == []


# ── AMEND-5.2B: claim-set consistency (not first-issuer-wins) ────────────────

def test_collect_query_claims_gathers_all_explicit_claims():
    from catalyst_agents.nodes.miner import collect_query_claims

    claims = collect_query_claims("Microsoft beat Apple after NVIDIA guidance", None)
    tickers = {claim.ticker for claim in claims}
    assert "MSFT" in tickers
    assert "AAPL" in tickers
    assert "NVDA" in tickers


def test_lone_taiwan_is_not_a_tsm_claim_but_phrase_and_symbol_are():
    from catalyst_agents.nodes.miner import collect_query_claims

    assert {claim.ticker for claim in collect_query_claims("Taiwan tensions rose overnight", None)} == set()
    assert collect_query_claims("Taiwan tensions rose overnight", None) == ()
    assert {claim.ticker for claim in collect_query_claims("Why did TSM move?", None)} == {"TSM"}
    assert {claim.ticker for claim in collect_query_claims("Why did Taiwan Semiconductor move?", None)} == {"TSM"}


def test_decide_consistent_when_structured_ticker_is_direct_target():
    from catalyst_agents.nodes.miner import collect_query_claims
    from catalyst_data.retrieval.query_policy import decide_claim_consistency

    cases = (
        ("Why did AAPL move after MSFT earnings?", "AAPL"),
        ("Why did F move after TSLA deliveries?", "F"),
        ("Why did AMD move after NVDA guidance?", "AMD"),
        ("Why did MSFT move after AMZN numbers?", "MSFT"),
    )
    for query, structured in cases:
        claims = collect_query_claims(query, None)
        assert decide_claim_consistency(claims, structured, query=query) is True, query


def test_decide_empty_claims_fail_open():
    from catalyst_agents.nodes.miner import collect_query_claims
    from catalyst_data.retrieval.query_policy import decide_claim_consistency

    assert decide_claim_consistency(collect_query_claims("", None), "AAPL", query="") is None


def test_decide_single_foreign_direct_target_is_mismatch():
    from catalyst_agents.nodes.miner import collect_query_claims
    from catalyst_data.retrieval.query_policy import decide_claim_consistency

    query = "Why did TSLA move?"
    claims = collect_query_claims(query, None)
    assert decide_claim_consistency(claims, "AAPL", query=query) is False


def test_decide_multiple_foreign_issuers_is_not_mismatch():
    from catalyst_agents.nodes.miner import collect_query_claims
    from catalyst_data.retrieval.query_policy import decide_claim_consistency

    # Ambiguous: two others, structured absent → fail-open, not mismatch.
    query = "Microsoft and Nvidia both missed."
    claims = collect_query_claims(query, None)
    assert decide_claim_consistency(claims, "AAPL", query=query) is None


def test_miner_peer_mentions_stay_consistent_and_retrieve():
    """Microsoft mentioned with structured Apple must invoke retrieval."""
    retriever = FixtureRetriever()
    cutoff_policy = RecordingCutoffPolicy()
    out = miner(
        _state("Why did Apple drop after Microsoft earnings?"),
        retriever=retriever,
        cutoff_policy=cutoff_policy,
    )
    assert out["ticker_consistent"] is True
    assert retriever.calls
    assert cutoff_policy.calls


@pytest.mark.parametrize(
    "query,ticker",
    [
        ("Why did Apple drop after Microsoft earnings?", "AAPL"),
        ("Ford missed after Tesla deliveries", "F"),
        ("AMD slipped after Nvidia guidance", "AMD"),
        ("Microsoft reacted after Amazon cloud numbers", "MSFT"),
    ],
)
def test_miner_named_peer_regressions_stay_consistent(query, ticker):
    retriever = FixtureRetriever()
    cutoff_policy = RecordingCutoffPolicy()
    state = {
        "ticker": ticker,
        "trade_date": "2026-01-15",
        "query": query,
        "price_move_pct": None,
        "corpus_manifest_id": "corpus-fixture-v1",
    }
    out = miner(state, retriever=retriever, cutoff_policy=cutoff_policy)
    # New direct-target policy: peer/event subjects that are not a high
    # confidence price-move subject fail open; never a hard reject.
    assert out["ticker_consistent"] is not False, (query, ticker, out)
    assert out.get("query_ticker_raw") in (None, ticker.upper()), (query, ticker, out)
    assert retriever.calls
    assert cutoff_policy.calls


def test_miner_taiwan_tensions_with_apple_fails_open_not_mismatch():
    retriever = FixtureRetriever()
    cutoff_policy = RecordingCutoffPolicy()
    out = miner(
        _state("Taiwan tensions hit supply chains overnight"),
        retriever=retriever,
        cutoff_policy=cutoff_policy,
    )
    assert out.get("ticker_consistent") is not False
    assert out.get("query_ticker_raw") is None
    assert retriever.calls
    assert cutoff_policy.calls


def test_miner_multi_foreign_issuers_without_target_is_not_mismatch():
    retriever = FixtureRetriever()
    cutoff_policy = RecordingCutoffPolicy()
    out = miner(
        _state("Microsoft and Nvidia both missed after guidance"),
        retriever=retriever,
        cutoff_policy=cutoff_policy,
    )
    assert out.get("ticker_consistent") is not False
    assert retriever.calls
    assert cutoff_policy.calls


def test_miner_single_foreign_issuer_still_short_circuits():
    retriever = FixtureRetriever()
    cutoff_policy = RecordingCutoffPolicy()
    out = miner(
        _state("Why did TSLA drop 3% on June 12, 2025?"),
        retriever=retriever,
        cutoff_policy=cutoff_policy,
    )
    assert out["ticker_consistent"] is False
    assert out["query_ticker_raw"] == "TSLA"
    assert out.get("provider_calls", 0) == 0
    assert out.get("retrieval_calls", 0) == 0
    assert retriever.calls == []
    assert cutoff_policy.calls == []


# ── AMEND-5.2C P2: provenance-aware ticker/issuer claims (fail-open by design) ─

def _miner_run(state_overrides=None, query=None, ticker="AAPL"):
    retriever = FixtureRetriever()
    cutoff_policy = RecordingCutoffPolicy()
    state = _state(query)
    state["ticker"] = ticker
    if state_overrides:
        state.update(state_overrides)
    out = miner(state, retriever=retriever, cutoff_policy=cutoff_policy)
    return out, retriever, cutoff_policy


def test_claim_provenance_sources_are_typed():
    from catalyst_agents.nodes.miner import collect_query_claims

    claims = collect_query_claims(
        "Why did Apple fall after $TSLA earnings and NYSE:F weakness?", None
    )
    by_ticker = {claim.ticker: claim.source for claim in claims}
    assert by_ticker["AAPL"] == "issuer_brand"
    assert by_ticker["TSLA"] == "marked_symbol"
    assert by_ticker["F"] == "marked_symbol"
    for claim in claims:
        assert claim.raw_text
        assert claim.end > claim.start >= 0
        assert claim.ticker


def test_claim_provenance_records_exact_spans():
    from catalyst_agents.nodes.miner import collect_query_claims

    text = "Why did TSLA move?"
    claims = collect_query_claims(text, None)
    tsla = [c for c in claims if c.ticker == "TSLA"][0]
    assert tsla.source == "explicit_symbol"
    assert text[tsla.start:tsla.end] == "TSLA"


def test_decision_matrix_hard_mismatches():
    from catalyst_data.retrieval.query_policy import decide_claim_consistency
    from catalyst_agents.nodes.miner import collect_query_claims

    cases = (
        ("Why did TSLA move?", "AAPL"),
        ("Why did $TSLA move?", "AAPL"),
        ("Why did NASDAQ:TSLA move?", "AAPL"),
        ("Why did $F move?", "AAPL"),
        ("Why did NYSE:F move?", "AAPL"),
    )
    for query, structured in cases:
        claims = collect_query_claims(query, None)
        assert decide_claim_consistency(claims, structured, query=query) is False, query


def test_decision_matrix_fail_open():
    from catalyst_data.retrieval.query_policy import decide_claim_consistency
    from catalyst_agents.nodes.miner import collect_query_claims

    consistent = (
        ("Why did Apple fall after TSLA earnings?", "AAPL"),  # target present
        ("Did Nvidia guidance hurt AMD?", "AMD"),              # target present
    )
    for query, structured in consistent:
        assert decide_claim_consistency(collect_query_claims(query, None), structured, query=query) is True, query

    cases = (
        ("Did Intel supply issues matter?", "AAPL"),           # issuer brand only
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
        ("Why did C move on 2025-07-24?", "AAPL"),  # bare single-letter
        ("Why did F move on 2025-07-24?", "AAPL"),  # bare single-letter
    )
    for query, structured in cases:
        claims = collect_query_claims(query, None)
        assert decide_claim_consistency(claims, structured, query=query) is None, (query, claims)


def test_hard_mismatch_query_ticker_raw_is_unique_foreign_symbol():
    out, retriever, cutoff_policy = _miner_run(
        query="Why did NASDAQ:TSLA move?", ticker="AAPL"
    )
    assert out["ticker_consistent"] is False
    assert out["query_ticker_raw"] == "TSLA"
    assert out.get("provider_calls", 0) == 0
    assert out.get("retrieval_calls", 0) == 0
    assert retriever.calls == []
    assert cutoff_policy.calls == []


def test_marked_f_hard_mismatch_zero_calls():
    for query in ("Why did $F move?", "Why did NYSE:F move?"):
        out, retriever, cutoff_policy = _miner_run(query=query, ticker="AAPL")
        assert out["ticker_consistent"] is False, query
        assert out["query_ticker_raw"] == "F", query
        assert retriever.calls == [], query
        assert cutoff_policy.calls == [], query


def test_apple_and_tsla_stays_consistent_and_retrieves():
    out, retriever, cutoff_policy = _miner_run(
        query="Why did Apple fall after TSLA earnings?", ticker="AAPL"
    )
    assert out["ticker_consistent"] is True
    assert out["query_ticker_raw"] == "AAPL"
    assert retriever.calls
    assert cutoff_policy.calls


def test_nvidia_and_amd_stays_consistent_and_retrieves():
    out, retriever, cutoff_policy = _miner_run(
        query="Did Nvidia guidance hurt AMD?", ticker="AMD"
    )
    assert out["ticker_consistent"] is True
    assert out["query_ticker_raw"] == "AMD"
    assert retriever.calls
    assert cutoff_policy.calls


@pytest.mark.parametrize(
    "query",
    [
        "Did Intel supply issues matter?",
        "new intel",
        "Form F-1",
        "vitamin C",
        "section C",
        "plain C",
        "plain F",
        "Microsoft and Nvidia both missed",
        "Taiwan Semiconductor",
        "APPL dropped about 3% on June 12, 2025",
    ],
)
def test_miner_ambiguous_or_brand_queries_fail_open(query):
    out, retriever, cutoff_policy = _miner_run(query=query, ticker="AAPL")
    assert out.get("ticker_consistent") is not False, (query, out)
    assert out.get("query_ticker_raw") is None, (query, out)
    assert retriever.calls, query
    assert cutoff_policy.calls, query


def test_miner_injected_query_ticker_raw_without_provenance_not_hard_reject():
    """state.query_ticker_raw has no provenance from production (runner seeds
    None); an injected raw string must not be treated as a strong explicit
    symbol and hard-reject a query that contains no such claim."""
    out, retriever, cutoff_policy = _miner_run(
        query="Why did the stock move after earnings?",
        ticker="AAPL",
        state_overrides={"query_ticker_raw": "TSLA"},
    )
    assert out.get("ticker_consistent") is not False
    assert out.get("query_ticker_raw") is None
    assert retriever.calls
    assert cutoff_policy.calls


# ── AMEND-5.2C follow-up P1: direct-target intent (foreign symbol context) ────

def test_miner_tsla_price_cuts_competitor_context_fails_open():
    """$TSLA as a competitor/context mention must not hard-reject AAPL."""
    out, retriever, cutoff_policy = _miner_run(
        query="Did $TSLA price cuts hurt the company?", ticker="AAPL",
    )
    assert out.get("ticker_consistent") is not False, out
    assert out.get("query_ticker_raw") is None
    assert retriever.calls
    assert cutoff_policy.calls


def test_miner_tsla_direct_target_after_structured_context_hard_mismatch():
    """TSLA is the direct movement subject even when AAPL appears as context."""
    out, retriever, cutoff_policy = _miner_run(
        query="Why did TSLA move after AAPL earnings?", ticker="AAPL",
    )
    assert out["ticker_consistent"] is False
    assert out["query_ticker_raw"] == "TSLA"
    assert out.get("provider_calls", 0) == 0
    assert out.get("retrieval_calls", 0) == 0
    assert retriever.calls == []
    assert cutoff_policy.calls == []


def test_miner_tsla_price_cuts_hurt_structured_target_never_false():
    """When the structured target is the impacted entity, never hard-reject."""
    out, retriever, cutoff_policy = _miner_run(
        query="Did TSLA price cuts hurt AAPL?", ticker="AAPL",
    )
    assert out.get("ticker_consistent") is not False, out
    assert retriever.calls
    assert cutoff_policy.calls


def test_miner_nvidia_guidance_hurt_amd_consistent():
    out, retriever, cutoff_policy = _miner_run(
        query="Did Nvidia guidance hurt AMD?", ticker="AMD",
    )
    assert out["ticker_consistent"] is True
    assert out["query_ticker_raw"] == "AMD"
    assert retriever.calls
    assert cutoff_policy.calls


@pytest.mark.parametrize(
    "query",
    [
        "Did $TSLA price cuts hurt the company?",
        "Did TSLA price cuts hurt AAPL?",
        "Was NVDA guidance responsible for the selected company's decline?",
        "Compare AAPL with TSLA.",
        "Apple fell after TSLA earnings.",
        "Did Nvidia guidance hurt AMD?",
        "Microsoft and Nvidia both missed.",
        "Form F-1",
        "vitamin C",
        "section C",
        "plain C",
        "plain F",
    ],
)
def test_miner_followup_fail_open_queries_never_hard_reject(query):
    out, retriever, cutoff_policy = _miner_run(query=query, ticker="AAPL")
    assert out.get("ticker_consistent") is not False, (query, out)
    assert retriever.calls, query
    assert cutoff_policy.calls, query


@pytest.mark.parametrize(
    "query",
    [
        "Why did TSLA move?",
        "Why did $TSLA fall?",
        "What caused TSLA to rise?",
        "Why was TSLA down 8%?",
        "TSLA fell 8% today — why?",
        "What drove TSLA's move?",
    ],
)
def test_miner_followup_direct_target_sentences_hard_mismatch(query):
    out, retriever, cutoff_policy = _miner_run(query=query, ticker="AAPL")
    assert out["ticker_consistent"] is False, (query, out)
    assert out["query_ticker_raw"] == "TSLA", (query, out)
    assert out.get("provider_calls", 0) == 0, query
    assert out.get("retrieval_calls", 0) == 0, query
    assert retriever.calls == [], query
    assert cutoff_policy.calls == [], query
