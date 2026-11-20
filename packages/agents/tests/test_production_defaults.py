"""The graph under the production attribution policy (verifier on, corrective off).

The suite-wide conftest pins the pre-verifier policy for the older graph tests;
these tests pass the production policy explicitly, so they cover the default
composition regardless of that fixture.
"""
from __future__ import annotations

import json

from catalyst_agents.attribution.answer_snapshot import ANSWER_BASIS_NOTE
from catalyst_agents.attribution.policy import AttributionPolicy
from catalyst_agents.attribution.policy import resolve_attribution_policy as _resolve


def resolve_attribution_policy(env, **kwargs):
    """These tests exercise the pre_close_v1 (P) graph path; the default is desk_cascade_v1."""
    return _resolve(env, **{"evidence_window_policy": "pre_close_v1", **kwargs})
from catalyst_agents.graphs.graph import run_v1_graph
from test_v1_graph import (
    GraphAnalystProvider,
    GraphWriterProvider,
    _abstain_decision,
    _desk_extraction,
    _desk_graph_kwargs,
    _follow_up_decision,
    _graph_kwargs,
    _ready_decision,
    _writer_text_factory,
)


class VerifyingAnalyst(GraphAnalystProvider):
    """Structured calls get the decision; the verifier's plain prompt gets a verdict."""

    def __init__(self, decision_factory, verdict: str):
        super().__init__(decision_factory)
        self.verdict = verdict
        self.verifier_calls = 0

    def invoke(self, messages):
        if isinstance(messages, str):
            self.verifier_calls += 1
            return json.dumps({"verdict": self.verdict, "category": "COMPANY_NEWS", "reason": "r"})
        return super().invoke(messages)


def _run(decision_factory, verdict: str, policy: AttributionPolicy | None = None):
    analyst = VerifyingAnalyst(decision_factory, verdict)
    kwargs = _graph_kwargs(analyst=analyst, writer=GraphWriterProvider(_writer_text_factory()))
    kwargs["query_builder"] = lambda task, ticker: "corrective " + task.evidence_need.value
    kwargs["attribution_policy"] = policy or resolve_attribution_policy({})
    return run_v1_graph(**kwargs), analyst


def test_default_policy_is_verifier_on_corrective_off() -> None:
    policy = resolve_attribution_policy({})
    assert policy.verifier_enabled is True
    assert policy.corrective_enabled is False
    ablation = resolve_attribution_policy(
        {"CATALYST_PRIMARY_VERIFIER": "0", "CATALYST_SKIP_CORRECTIVE": "0"}
    )
    assert ablation.verifier_enabled is False and ablation.verifier_version is None
    assert ablation.corrective_enabled is True


def test_desk_cascade_stage_one_publication_reports_attempted_and_published_stage():
    kwargs, _ = _desk_graph_kwargs([_desk_extraction(publish=True)])
    result = run_v1_graph(**kwargs)
    assert result.answer_snapshot["cascade_stage"] == 1
    assert result.answer_snapshot["last_attempted_stage"] == 1
    assert result.answer_snapshot["published_stage"] == 1
    assert result.answer_snapshot["basis_note"] == ANSWER_BASIS_NOTE
    assert ANSWER_BASIS_NOTE == "The cited source text is shown below. The system checked each quote against its source and the applicable evidence window."
    assert ANSWER_BASIS_NOTE not in result.answer.text


def test_desk_cascade_stage_two_publication_reports_attempted_and_published_stage(monkeypatch):
    monkeypatch.setenv("CATALYST_CASCADE_STAGE2", "1")
    import test_v1_graph as graph_test
    from catalyst_agents.attribution.policy import resolve_attribution_policy
    from catalyst_data.canonical.temporal import with_event_time_publication_window

    kwargs, fake = _desk_graph_kwargs([_desk_extraction(publish=False), _desk_extraction(publish=True)])
    kwargs["attribution_policy"] = resolve_attribution_policy(
        {"CATALYST_PRIMARY_VERIFIER": "0"}, evidence_window_policy="desk_cascade_v1",
        web_evidence="discovery_v2",
    )
    kwargs["stage2_temporal_identity"] = with_event_time_publication_window(graph_test._temporal())
    kwargs["stage2_retriever"] = graph_test.DeskGraphRetriever(fake)
    result = run_v1_graph(**kwargs)
    assert result.answer_snapshot["cascade_stage"] == 2
    assert result.answer_snapshot["last_attempted_stage"] == 2
    assert result.answer_snapshot["published_stage"] == 2


def test_desk_cascade_stage_one_abstention_reports_null_published_stage():
    kwargs, _ = _desk_graph_kwargs([_desk_extraction(publish=False)])
    result = run_v1_graph(**kwargs)
    assert result.answer_snapshot["cascade_stage"] == 1
    assert result.answer_snapshot["last_attempted_stage"] == 1
    assert result.answer_snapshot["published_stage"] is None
    assert result.answer_snapshot["basis_note"] is None


def test_supported_primary_is_published_and_the_verifier_call_is_counted() -> None:
    result, analyst = _run(_ready_decision, "SUPPORTED")
    assert analyst.verifier_calls == 1
    assert result.verifier_logical_calls == 1
    assert result.verifier_provider_attempts == 1
    assert result.logical_model_call_count == 2  # Analyst + verifier
    assert result.terminal_envelope["final_status"] == "SUFFICIENT"


def test_normal_path_snapshot_has_no_web_source_metadata() -> None:
    result, _ = _run(_ready_decision, "SUPPORTED")
    assert "evidence_origin" not in result.answer_snapshot
    assert "label" not in result.answer_snapshot
    assert "domain" not in result.answer_snapshot["sources"][0]


def test_normal_supported_verdict_keeps_existing_partial_answered_outcome() -> None:
    def partial_decision():
        decision = _ready_decision()
        decision["recommended_status"] = "PARTIAL"
        decision["proposed_missing_evidence"] = [
            {
                "proposal_ref": "primary-confirmation",
                "evidence_need": "COMPANY_PRIMARY",
                "time_scope": "PRIOR_SESSION",
                "expected_information": "issuer confirmation",
                "reason_code": "MISSING_PRIMARY_CONFIRMATION",
            },
            {
                "proposal_ref": "independent-corroboration",
                "evidence_need": "COMPANY_NEWS",
                "time_scope": "PRIOR_SESSION",
                "expected_information": "independent reporting",
                "reason_code": "MISSING_INDEPENDENT_CORROBORATION",
            },
        ]
        return decision

    result, analyst = _run(partial_decision, "SUPPORTED")

    assert analyst.verifier_calls == 1
    assert result.answer_snapshot["status"] == "PARTIAL"
    assert result.answer_snapshot["outcome"] == "ANSWERED"
    assert "abstention_reason" not in result.decision_trace


def test_user_selected_web_source_runs_normal_checks_and_labels_snapshot() -> None:
    from dataclasses import replace

    analyst = VerifyingAnalyst(_ready_decision, "SUPPORTED")
    kwargs = _graph_kwargs(analyst=analyst, writer=GraphWriterProvider(_writer_text_factory()))
    retriever = kwargs["retriever"]
    original_evidence = retriever._evidence
    retriever._evidence = lambda evidence_id, rank: replace(
        original_evidence(evidence_id, rank), canonical_url="https://www.sec.gov/Archives/example"
    )
    kwargs["attribution_policy"] = resolve_attribution_policy({})
    kwargs["web_source_title"] = "Apple reports record revenue and guidance"
    kwargs["web_source_company_name"] = "Apple Inc."
    result = run_v1_graph(**kwargs)

    assert analyst.calls == 1
    assert analyst.verifier_calls == 1
    assert result.answer_snapshot["evidence_origin"] == "web_search"
    assert result.answer_snapshot["label"] == "user-selected web source"
    assert result.answer_snapshot["sources"][0]["domain"] == "www.sec.gov"
    assert result.answer_snapshot["outcome"] == "ANSWERED"


def test_user_selected_source_without_company_in_page_title_abstains() -> None:
    kwargs = _graph_kwargs(
        analyst=VerifyingAnalyst(_ready_decision, "SUPPORTED"),
        writer=GraphWriterProvider(_writer_text_factory()),
    )
    kwargs["attribution_policy"] = resolve_attribution_policy({})
    kwargs["web_source_title"] = "A new corporate filing"
    kwargs["web_source_company_name"] = "Apple Inc."
    result = run_v1_graph(**kwargs)
    assert result.answer_snapshot["outcome"] == "ABSTAINED"
    assert "title and quote requirements" in " ".join(result.answer_snapshot["limitations"])


def test_rejected_primary_abstains() -> None:
    result, _ = _run(_ready_decision, "REJECT")
    assert result.terminal_envelope["final_status"] == "ABSTAIN"
    assert result.terminal_envelope["assured"] is True


def test_follow_up_does_not_run_a_corrective_round_by_default() -> None:
    result, analyst = _run(_follow_up_decision, "SUPPORTED")
    assert result.corrective_rounds == 0
    assert analyst.calls == 1  # one Analyst call, no second round


def test_verifier_off_makes_no_verifier_call() -> None:
    policy = resolve_attribution_policy({"CATALYST_PRIMARY_VERIFIER": "0"})
    result, analyst = _run(_ready_decision, "REJECT", policy)
    assert analyst.verifier_calls == 0
    assert result.verifier_logical_calls == 0
    assert result.terminal_envelope["final_status"] == "SUFFICIENT"


def test_decision_trace_records_each_stage_and_decision() -> None:
    result, _ = _run(_ready_decision, "REJECT")
    trace = result.decision_trace
    assert trace["schema_version"] == "decision_trace_v1"
    assert [s["stage"] for s in trace["stages"]] == [
        "research_and_context_pack", "evidence_analyst", "checks_and_verifier", "claims_and_render",
    ]
    assert all("duration_ms" in s for s in trace["stages"])
    assert trace["retrieval"]["included_in_pack"] >= 1
    (round1,) = trace["rounds"]
    analyst = round1["model_reported"]["analyst"]
    assert analyst["evidence_decisions"][0]["disposition"] == "SUPPORT"
    assert analyst["hypotheses"][0]["quotes"][0]["evidence_id"] == "e1"
    assert round1["code_checks"]["quotes"][0]["kept"] is True
    assert [g["gate"] for g in round1["code_checks"]["gates"]] == ["comove", "subject", "timing"]
    verifier = round1["model_reported"]["verifier"]
    assert verifier["called"] and verifier["verdict"] == "REJECT" and verifier["category"] == "COMPANY_NEWS"
    assert round1["code_checks"]["outcome"]["final_status"] == "ABSTAIN"
    assert trace["final_status"] == "ABSTAIN"
    assert trace["model_calls"] == {"analyst": 1, "verifier": 1, "writer": 0}


def test_decision_trace_retrieval_adds_readable_task_names_without_changing_old_keys() -> None:
    from types import SimpleNamespace

    from catalyst_agents.observability.decision_trace import DecisionTrace

    def result(task_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            task_id=task_id, status="SUCCEEDED", latency_ms=4, evidence_items=(1,),
            structured_facts=(), mode_served="hybrid", degradation_reasons=(),
        )

    priority_id = "research:1:0:priority"
    lead_id = "research:1:lead:followup"
    priority = SimpleNamespace(
        task_id=priority_id, evidence_need="COMPANY_NEWS",
        time_scope="SESSION_INFORMATION_WINDOW",
    )
    lead = SimpleNamespace(
        task_id=lead_id, evidence_need="COMPANY_PRIMARY", time_scope="PRIOR_SESSION",
    )
    evidence_state = SimpleNamespace(task_results=(result(priority_id), result(lead_id)),
                                     evidence_items=(), structured_facts=())
    context_pack = SimpleNamespace(
        research_history=(priority, lead), evidence_inventory=(), included_evidence_ids=(),
        excluded_evidence_ids=(), lead_only_evidence=(),
    )

    trace = DecisionTrace("run:test")
    trace.retrieval(evidence_state=evidence_state, context_pack=context_pack,
                    research_tasks=context_pack.research_history)

    priority_record, lead_record = trace.payload["retrieval"]["tasks"]
    old_keys = {"task_id", "status", "latency_ms", "items", "facts", "mode_served",
                "degradation_reasons"}
    assert set(priority_record) == old_keys | {"evidence_need", "time_scope"}
    assert {key: priority_record[key] for key in old_keys} == {
        "task_id": priority_id, "status": "SUCCEEDED", "latency_ms": 4, "items": 1,
        "facts": 0, "mode_served": "hybrid", "degradation_reasons": [],
    }
    assert priority_record["evidence_need"] == "COMPANY_NEWS"
    assert priority_record["time_scope"] == "SESSION_INFORMATION_WINDOW"
    assert lead_record["evidence_need"] == "LEAD_FOLLOW_UP"
    assert lead_record["time_scope"] == "PRIOR_SESSION"


def test_decision_trace_says_why_the_verifier_did_not_run() -> None:
    policy = resolve_attribution_policy({"CATALYST_PRIMARY_VERIFIER": "0"})
    result, _ = _run(_ready_decision, "SUPPORTED", policy)
    verifier = result.decision_trace["rounds"][0]["model_reported"]["verifier"]
    assert verifier == {
        "called": False,
        "skipped_because": "verifier_off",
        "primary_proposal_state": "PROPOSED_QUOTES_KEPT",
    }


def test_answer_snapshot_is_structured_and_answer_bytes_remain_stable() -> None:
    result, _ = _run(_ready_decision, "SUPPORTED")
    snapshot = result.answer_snapshot
    assert snapshot["schema_version"] == "answer_snapshot_v1"
    assert snapshot["outcome"] == "ANSWERED"
    assert snapshot["status"] == "SUFFICIENT"
    assert snapshot["key_sentence"] is not None
    assert snapshot["key_sentence"]["text"] in result.answer.text
    assert snapshot["sources"][0]["role"] == "PRIMARY"
    assert snapshot["headline"].startswith("AAPL rose 10.00%")
    assert "\n## CAUSAL_EXPLANATION\n[PRIMARY]" in result.answer.text
    assert result.answer.text.endswith("\n")
    assert result.answer.text == (
        "## SUMMARY\n"
        "AAPL +10.00% on 2026-01-15; market +9.50%, sector ETF XLK +9.20%, peer median +9.70% across 2 peers. "
        "Co-movement computed from closing prices: in line with the market; in line with the sector; in line with the peers. "
        "Timing: opening gap +2.00%, open to close +7.84%.\n"
        "The cause lines below quote source documents verbatim; each quote was checked against its cited evidence and the session window. "
        "Which sentences were selected, and the cause category, are model judgments.\n\n"
        "## CAUSAL_EXPLANATION\n"
        "[PRIMARY] (claim_id: claim:78406fd6b6979a97) “Apple reported record revenue and raised its full-year guidance.” — company filing, "
        "2026-01-15 10:00 UTC [citations: e1]\n\n"
        "## LIMITATIONS\n- No additional limitations were required.\n"
    )


def test_no_primary_abstention_snapshot_has_specific_reason() -> None:
    result = run_v1_graph(**_graph_kwargs(
        analyst=GraphAnalystProvider(_abstain_decision),
        writer=GraphWriterProvider(_writer_text_factory()),
    ))
    assert result.answer_snapshot["outcome"] == "ABSTAINED"
    assert result.answer_snapshot["basis_note"] is None
    assert "No cause was published for this move: no source sentence within the evidence window was accepted as its cause." in result.answer.text
    assert "No cause was accepted:" not in result.answer.text
    assert result.answer_snapshot["limitations"][0] == (
        "No cause was published for this move: no source sentence within the evidence window was accepted as its cause."
    )
    assert result.answer_snapshot["abstention"]["reason_code"] == "NO_PRIMARY_PROPOSED"
    assert result.decision_trace["abstention_reason"] == {
        "reason_code": "NO_PRIMARY_PROPOSED", "details": {},
    }
    assert result.answer_snapshot["abstention"]["reason_text"] == (
        "No primary cause was proposed for publication."
    )


def test_answer_snapshot_keeps_primary_and_secondary_quotes_in_render_order() -> None:
    payload = _ready_decision("e1")
    payload["evidence_decisions"][0]["supports_hypothesis_refs"].append("h2")
    payload["candidate_hypotheses"].append({
        "hypothesis_ref": "h2", "cause_type": "COMPANY_SPECIFIC_CATALYST",
        "statement": "AAPL also had a separate guidance catalyst.",
        "quotes": [{"evidence_id": "e1", "text": "Reference e1 was included in the filing."}],
        "supporting_evidence_ids": ["e1"], "magnitude_fit": "STRONG",
        "proposed_role": "SECONDARY",
    })
    result, _ = _run(lambda: payload, "SUPPORTED")
    snapshot = result.answer_snapshot
    assert [source["role"] for source in snapshot["sources"]] == ["PRIMARY", "SECONDARY"]
    assert snapshot["key_sentence"]["text"] in result.answer.text
    assert snapshot["also_sentence"]["text"] in result.answer.text


def test_abstention_snapshot_records_unlinked_possible_explanation() -> None:
    class UnlinkedAnalyst(VerifyingAnalyst):
        def invoke(self, messages):
            if isinstance(messages, str):
                self.verifier_calls += 1
                return json.dumps({"verdict": "REJECT", "category": "UNLINKED", "reason": "No causal link was established."})
            return GraphAnalystProvider.invoke(self, messages)

    analyst = UnlinkedAnalyst(_ready_decision, "REJECT")
    kwargs = _graph_kwargs(analyst=analyst, writer=GraphWriterProvider(_writer_text_factory()))
    kwargs["attribution_policy"] = resolve_attribution_policy({})
    result = run_v1_graph(**kwargs)
    possible = result.answer_snapshot["abstention"]["possible_explanation"]
    assert result.answer_snapshot["outcome"] == "ABSTAINED"
    assert result.answer_snapshot["abstention"]["reason_code"] == "VERIFIER_REJECTED"
    assert result.answer_snapshot["abstention"]["details"]["verifier_reason"] == "UNLINKED"
    assert result.decision_trace["abstention_reason"]["reason_code"] == "VERIFIER_REJECTED"
    assert possible["verifier_reason"] == "No causal link was established."
    assert possible["quotes"][0]["text"] in result.answer.text


def test_abstention_snapshot_leads_match_rendered_related_news(monkeypatch) -> None:
    from catalyst_agents.attribution import abstain_context

    leads = [{"title": "Apple supplier update", "publisher": "Example News",
              "published_at": "2026-01-15 11:00 UTC", "url": "https://example.test/story"}]
    monkeypatch.setattr(abstain_context, "related_news_items", lambda _pack, _ticker: leads)
    result = run_v1_graph(**_graph_kwargs(
        analyst=GraphAnalystProvider(_abstain_decision),
        writer=GraphWriterProvider(_writer_text_factory()),
    ))
    lines = result.answer.text.splitlines()
    start = lines.index("## RELATED_NEWS — leads, not causes") + 1
    related_lines = lines[start:]
    if "" in related_lines:
        related_lines = related_lines[:related_lines.index("")]
    rendered = [line[2:] for line in related_lines if line.startswith("- ")]
    expected = [f"{item['title']} — {item['publisher']}, {item['published_at']}, {item['url']}" for item in leads]
    assert result.answer_snapshot["abstention"]["possible_explanation"] is None
    assert result.answer_snapshot["abstention"]["leads"] == leads
    assert rendered == expected


def test_abstention_snapshot_distinguishes_primary_quotes_failed() -> None:
    payload = _ready_decision("e1")
    payload["candidate_hypotheses"][0]["quotes"][0]["text"] = "This sentence is not in the evidence excerpt."
    result = run_v1_graph(**_graph_kwargs(
        analyst=GraphAnalystProvider(lambda: payload),
        writer=GraphWriterProvider(_writer_text_factory()),
    ))
    verifier = result.decision_trace["rounds"][0]["model_reported"]["verifier"]
    assert verifier["primary_proposal_state"] == "PROPOSED_QUOTES_FAILED"
    assert result.answer_snapshot["abstention"]["reason_code"] == "PRIMARY_QUOTES_FAILED"
    assert result.answer_snapshot["abstention"]["details"]["failed_quote_rule_codes"]


def test_abstention_structures_render_the_same_text() -> None:
    from types import SimpleNamespace

    from catalyst_agents.attribution import abstain_context

    quote = {"text": "Acme shares slid after the report.", "publisher": "Benzinga",
             "published_at": "2026-03-02 15:00 UTC", "url": "https://www.benzinga.com/a", "evidence_id": "web:a:web_body:0001"}
    verifier = SimpleNamespace(last={"verdict": False, "category": "UNLINKED", "reason": "nothing ties it to the move",
                                     "statement": "x"})
    content = {"possible_explanation": {"quotes": [quote], "verifier_reason": "nothing ties it to the move"},
               "leads": [{"title": "Acme in focus", "publisher": "Yahoo", "published_at": "2026-03-02 14:00 UTC",
                          "url": "https://finance.yahoo.com/a"}]}
    text = abstain_context.render_structured(content)
    assert "## POSSIBLE_EXPLANATION — not established" in text and quote["text"] in text
    assert "Not established: nothing ties it to the move" in text
    assert "- Acme in focus — Yahoo, 2026-03-02 14:00 UTC, https://finance.yahoo.com/a" in text
    leads_only = abstain_context.render_structured({"possible_explanation": None, "leads": content["leads"]})
    assert "POSSIBLE_EXPLANATION" not in leads_only and "RELATED_NEWS" in leads_only
    assert abstain_context.render_structured({"possible_explanation": None, "leads": []}) == ""
    # A non-UNLINKED rejection never yields an explanation.
    other = SimpleNamespace(last={**verifier.last, "category": "RECAP"})
    pack = SimpleNamespace(evidence_inventory=[], temporal_identity=None)
    assert abstain_context.structured(other, pack, "ACME", [quote])["possible_explanation"] is None


def test_headline_uses_as_for_same_direction_and_while_otherwise() -> None:
    from types import SimpleNamespace

    from catalyst_agents.attribution.answer_snapshot import _headline

    p = SimpleNamespace(target_return=-3.2, sector_ticker="XLC", sector_return=-2.4, market_return=-0.3,
                        peer_summary=None, commodity_ticker=None, commodity_return=None)
    assert _headline("NFLX", p) == "NFLX fell 3.20% as its sector fell (XLC -2.40%)."
    p.sector_return = 0.39
    assert _headline("NFLX", p) == "NFLX fell 3.20% while its sector rose (XLC +0.39%)."


def test_renderer_text_is_exact_for_normal_filing_and_abstain_paths() -> None:
    from types import SimpleNamespace

    from catalyst_agents.attribution.analyst import AttributionStatus
    from catalyst_agents.nodes.quote_renderer import QuoteAnswerRenderer

    head = "The cause lines below quote source documents verbatim; each quote was checked against its cited evidence and the session window. "
    normal = "AAPL +1.00%\n" + head + "Which sentences were selected, and the cause category, are model judgments."
    filing = "AAPL +1.00%\n" + head + "In this case the quoted line was chosen by a fixed rule, not by a model."
    abstain = (
        "## OBSERVED_MOVE\nAAPL +1.00%\n\n## LIMITATIONS\n"
        "No cause was published for this move: no source sentence within the evidence window was accepted as its cause.\n"
        "- Coverage gap: X\n"
    )
    wi = SimpleNamespace(observed_move="AAPL +1.00%", final_status=AttributionStatus.SUFFICIENT)
    for basis in (None, "STAGE_1", "STAGE_2"):
        assert QuoteAnswerRenderer(wi, basis=basis).render() == normal
    assert QuoteAnswerRenderer(wi, basis="EARNINGS_FILING").render() == filing
    ab = SimpleNamespace(observed_move="AAPL +1.00%", final_status=AttributionStatus.ABSTAIN,
                         required_limitations=("Coverage gap: X",))
    assert QuoteAnswerRenderer(ab).render() == abstain
    assert QuoteAnswerRenderer(ab, basis="EARNINGS_FILING").render() == abstain
