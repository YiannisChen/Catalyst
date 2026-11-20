from datetime import datetime, timezone
from types import SimpleNamespace

from catalyst_agents.attribution.analyst import (
    AnalystDecision,
    AttributionStatus,
    AttributionType,
    CandidateHypothesis,
    CauseType,
    EvidenceDecision,
    EvidenceDisposition,
    HypothesisRole,
    MagnitudeFit,
    ResearchDecision,
    SourceQuote,
)
from catalyst_agents.attribution.answer_snapshot import classify_web_source_snapshot
from catalyst_agents.graphs.web_source_decision import web_source_decision


def _hypothesis(ref, quote, *, cause=CauseType.COMPANY_SPECIFIC_CATALYST, role=HypothesisRole.PRIMARY):
    return CandidateHypothesis(
        hypothesis_ref=ref,
        cause_type=cause,
        statement="The selected company event explains the move.",
        quotes=(SourceQuote(evidence_id="web:1", text=quote),),
        supporting_evidence_ids=("web:1",),
        magnitude_fit=MagnitudeFit.PLAUSIBLE,
        proposed_role=role,
    )


def test_web_source_requires_company_news_and_company_name_in_title_and_quote():
    decision = AnalystDecision(
        schema_version="analyst_decision_v1",
        evidence_decisions=(EvidenceDecision(
            evidence_id="web:1", disposition=EvidenceDisposition.SUPPORT,
            supports_hypothesis_refs=("good", "quote_misses", "wrong_type"),
        ),),
        candidate_hypotheses=(
            _hypothesis("good", "Tesla announced a new factory today."),
            _hypothesis("quote_misses", "The company announced a new factory today.", role=HypothesisRole.SECONDARY),
            _hypothesis("wrong_type", "Tesla announced a new factory today.", cause=CauseType.SECTOR_MOVE, role=HypothesisRole.SECONDARY),
        ),
        research_decision=ResearchDecision.READY,
        recommended_status=AttributionStatus.SUFFICIENT,
        proposed_attribution_type=AttributionType.EVIDENCE_BACKED_CAUSAL,
    )
    constrained = web_source_decision(
        decision, title="Tesla announces new factory", ticker="TSLA", company_name="Tesla, Inc."
    )
    assert [item.hypothesis_ref for item in constrained.candidate_hypotheses] == ["good"]
    no_title_match = web_source_decision(
        decision, title="Automaker announces new factory", ticker="TSLA", company_name="Tesla, Inc."
    )
    assert no_title_match.candidate_hypotheses == ()


def test_web_source_matches_full_issuer_name_without_accepting_generic_first_word():
    decision = AnalystDecision(
        schema_version="analyst_decision_v1",
        evidence_decisions=(EvidenceDecision(
            evidence_id="web:1", disposition=EvidenceDisposition.SUPPORT,
            supports_hypothesis_refs=("amd",),
        ),),
        candidate_hypotheses=(_hypothesis("amd", "Advanced Micro Devices announced a new product."),),
        research_decision=ResearchDecision.READY,
        recommended_status=AttributionStatus.SUFFICIENT,
        proposed_attribution_type=AttributionType.EVIDENCE_BACKED_CAUSAL,
    )

    accepted = web_source_decision(
        decision,
        title="Advanced Micro Devices announces a new product",
        ticker="AMD",
        company_name="Advanced Micro Devices, Inc.",
    )
    rejected = web_source_decision(
        decision,
        title="Advanced battery maker announces a new product",
        ticker="AMD",
        company_name="Advanced Micro Devices, Inc.",
    )

    assert len(accepted.candidate_hypotheses) == 1
    assert rejected.candidate_hypotheses == ()


def test_web_source_supported_requires_only_the_two_evidence_ceilings():
    snapshot = {
        "outcome": "ABSTAINED",
        "status": "PARTIAL",
        "headline": "TSLA fell 3.56% as its sector fell (XLY -1.00%).",
        "key_sentence": None,
        "sources": [],
        "limitations": [
            "No cause was published for this move: no source sentence within the evidence window was accepted as its cause.",
            "Coverage gap: MISSING_PRIMARY_CONFIRMATION for COMPANY_PRIMARY.",
            "Coverage gap: MISSING_INDEPENDENT_CORROBORATION for COMPANY_NEWS.",
            "Coverage gap: MISSING_SECTOR_CONTEXT for SECTOR_NEWS.",
        ],
        "abstention": {"reason_code": "CEILING_ABSTAIN", "reason_text": "ceiling"},
    }
    trace = {
        "rounds": [{
            "model_reported": {
                "analyst": {"hypotheses": [{
                    "ref": "primary-1", "proposed_role": "PRIMARY",
                    "cause_type": "COMPANY_SPECIFIC_CATALYST",
                }]},
                "verifier": {"called": True, "verdict": "SUPPORTED"},
            },
            "code_checks": {
                "quotes": [{"hypothesis_ref": "primary-1", "evidence_id": "ev-1",
                            "text": "Tesla raised its full-year guidance.", "kept": True}],
                "gates": [{"gate": name, "result": "no_change"}
                          for name in ("comove", "subject", "timing")],
            },
        }]
    }
    evidence = [SimpleNamespace(
        evidence_id="ev-1", canonical_url="https://news.example/tesla",
        publisher="Example News", source_class="company_news",
        eligible_at=datetime(2026, 4, 23, 16, tzinfo=timezone.utc),
    )]

    result = classify_web_source_snapshot(snapshot, trace, evidence)

    assert result["outcome"] == "WEB_SOURCE_SUPPORTED"
    assert result["headline"] == snapshot["headline"]
    assert result["key_sentence"]["text"] == "Tesla raised its full-year guidance."
    assert result["sources"] == [{
        "role": "PRIMARY", "text": "Tesla raised its full-year guidance.",
        "publisher": "Example News", "published_at": "2026-04-23 16:00 UTC",
        "url": "https://news.example/tesla", "evidence_id": "ev-1", "domain": "news.example",
    }]
    assert result["limitations"] == [
        "Single user-selected web source; not corroborated by a second independent or primary source.",
        "Coverage gap: MISSING_SECTOR_CONTEXT for SECTOR_NEWS.",
    ]
    assert "abstention" not in result


def test_web_source_failures_are_precise_and_only_no_primary_gets_title_warning():
    snapshot = {"outcome": "ABSTAINED", "limitations": [],
                "abstention": {"reason_code": "NO_PRIMARY_PROPOSED", "reason_text": "old"}}
    empty_trace = {"rounds": [{"model_reported": {"analyst": {"hypotheses": []}},
                                "code_checks": {"quotes": [], "gates": []}}]}

    result = classify_web_source_snapshot(snapshot, empty_trace, [], filter_removed_all=True)

    assert result["outcome"] == "ABSTAINED"
    assert result["abstention"]["reason_code"] == "NO_PRIMARY_PROPOSED"
    assert result["abstention"]["details"]["web_source_failure"] == "no_issuer_primary"
    assert result["limitations"] == [
        "The selected web source did not meet the company-news title and quote requirements."
    ]


def test_web_source_quote_gate_and_verifier_failures_have_specific_reasons():
    hypothesis = {"ref": "p", "proposed_role": "PRIMARY",
                  "cause_type": "COMPANY_SPECIFIC_CATALYST"}
    base_round = {
        "model_reported": {
            "analyst": {"hypotheses": [hypothesis]},
            "verifier": {"called": True, "verdict": "SUPPORTED"},
        },
        "code_checks": {
            "quotes": [{"hypothesis_ref": "p", "evidence_id": "e", "text": "Tesla grew.",
                        "kept": True}],
            "gates": [{"gate": "subject", "result": "no_change"}],
        },
    }
    evidence = [SimpleNamespace(evidence_id="e", canonical_url="https://news.example/tesla",
                                publisher="Example News", source_class="company_news",
                                eligible_at=datetime(2026, 4, 23, 16, tzinfo=timezone.utc))]

    quote_failure = {
        "rounds": [{**base_round, "code_checks": {**base_round["code_checks"], "quotes": []}}]
    }
    gate_failure = {
        "rounds": [{**base_round, "code_checks": {**base_round["code_checks"],
                    "gates": [{"gate": "subject", "result": "ceiling:ABSTAIN"}]}}]
    }
    verifier_failure = {
        "rounds": [{**base_round, "model_reported": {**base_round["model_reported"],
                    "verifier": {"called": True, "verdict": "REJECT", "reason": "not causal"}}}]
    }
    snapshots = [{"outcome": "ABSTAINED", "limitations": [
        "Coverage gap: MISSING_PRIMARY_CONFIRMATION for COMPANY_PRIMARY."]} for _ in range(3)]

    results = [
        classify_web_source_snapshot(snapshots[0], quote_failure, evidence),
        classify_web_source_snapshot(snapshots[1], gate_failure, evidence),
        classify_web_source_snapshot(snapshots[2], verifier_failure, evidence),
    ]

    assert [result["abstention"]["reason_code"] for result in results] == [
        "PRIMARY_QUOTES_FAILED", "GATE_FAILED", "VERIFIER_REJECTED",
    ]
    assert results[1]["abstention"]["details"]["failed_gate_names"] == ["subject"]
    assert results[2]["abstention"]["details"]["verifier_reason"] == "OTHER"
    assert all("title and quote requirements" not in " ".join(result["limitations"])
               for result in results)


def test_web_source_supported_is_not_allowed_for_normal_runs_with_same_verdicts():
    snapshot = {"outcome": "ABSTAINED", "status": "PARTIAL", "limitations": []}
    trace = {"rounds": [{
        "model_reported": {
            "analyst": {"hypotheses": [{"ref": "p", "proposed_role": "PRIMARY"}]},
            "verifier": {"called": True, "verdict": "SUPPORTED"},
        },
        "code_checks": {"quotes": [{"hypothesis_ref": "p", "evidence_id": "e",
                                     "text": "quote", "kept": True}],
                        "gates": [{"gate": "comove", "result": "no_change"}]},
    }]}

    result = classify_web_source_snapshot(snapshot, trace, [], web_source=False)

    assert result == snapshot
