from catalyst_agents.attribution.answer_snapshot import (
    ABSTENTION_REASON_CODES,
    _abstention_reason,
    abstention_reason_text,
    derive_abstention_reason,
)


def test_supported_verifier_with_policy_abstention_is_not_reported_as_no_verdict():
    trace = {
        "rounds": [
            {
                "model_reported": {
                    "verifier": {
                        "called": True,
                        "verdict": "SUPPORTED",
                        "primary_proposal_state": "PROPOSED_QUOTES_KEPT",
                    }
                }
            }
        ]
    }

    code, text = _abstention_reason(trace, assessment=None)

    assert code == "CEILING_ABSTAIN"
    assert text == "The evidence and policy checks limited the attribution to an abstention."


def test_abstention_reason_distinguishes_no_primary_from_failed_primary_quotes():
    no_primary = {"retrieval": {"evidence_in_state": 4}, "rounds": [{
        "model_reported": {"analyst": {"hypotheses": []},
                           "verifier": {"called": False, "skipped_because": "no_primary"}},
        "code_checks": {"quotes": []},
    }]}
    failed_quotes = {"retrieval": {"evidence_in_state": 4}, "rounds": [{
        "model_reported": {"analyst": {"hypotheses": [{"ref": "p", "proposed_role": "PRIMARY"}]},
                           "verifier": {"called": False, "skipped_because": "no_primary"}},
        "code_checks": {"quotes": [{"hypothesis_ref": "p", "kept": False,
                                     "rule": "R2_NOT_COMPLETE_SENTENCE"}]},
    }]}

    absent = derive_abstention_reason(no_primary)
    failed = derive_abstention_reason(failed_quotes)

    assert absent == {"reason_code": "NO_PRIMARY_PROPOSED", "details": {}}
    assert failed == {
        "reason_code": "PRIMARY_QUOTES_FAILED",
        "details": {"failed_quote_rule_codes": ["R2_NOT_COMPLETE_SENTENCE"]},
    }


def test_abstention_reason_reports_gate_verifier_and_ceiling_details():
    trace = {"retrieval": {"evidence_in_state": 2}, "rounds": [{
        "model_reported": {
            "analyst": {"recommended_status": "SUFFICIENT", "hypotheses": [
                {"ref": "p", "proposed_role": "PRIMARY"}]},
            "verifier": {"called": True, "verdict": "REJECT", "category": "UNLINKED",
                         "reason": "No evidence links the event to the move."},
        },
        "code_checks": {"quotes": [{"hypothesis_ref": "p", "kept": True, "rule": "OK"}],
                        "gates": [{"gate": "subject", "result": "ceiling:ABSTAIN"}],
                        "outcome": {"status_ceiling": "ABSTAIN"}},
    }]}

    reason = derive_abstention_reason(trace)

    assert reason == {"reason_code": "GATE_FAILED", "details": {"failed_gate_names": ["subject"]}}

    trace["rounds"][0]["code_checks"]["gates"] = [{"gate": "subject", "result": "no_change"}]
    reason = derive_abstention_reason(trace)
    assert reason == {
        "reason_code": "VERIFIER_REJECTED",
        "details": {"verifier_reason": "UNLINKED", "verifier_note": "No evidence links the event to the move."},
    }

    trace["rounds"][0]["model_reported"]["verifier"] = {
        "called": True, "verdict": "SUPPORTED", "category": "COMPANY_NEWS"}
    trace["rounds"][0]["code_checks"]["outcome"]["ceiling_codes"] = ["MISSING_PRIMARY_CONFIRMATION"]
    reason = derive_abstention_reason(trace)
    assert reason == {
        "reason_code": "CEILING_ABSTAIN",
        "details": {"ceiling_codes": ["MISSING_PRIMARY_CONFIRMATION"]},
    }


def test_abstention_reason_marks_empty_inventory_and_analyst_abstention():
    no_news = {"retrieval": {"included_in_pack": 0, "evidence_in_state": 0}, "rounds": []}
    already_abstained = {"retrieval": {"included_in_pack": 1}, "rounds": [{
        "model_reported": {
            "analyst": {"recommended_status": "ABSTAIN", "hypotheses": [
                {"ref": "p", "proposed_role": "PRIMARY"}]},
            "verifier": {"called": False, "skipped_because": "already_abstain"},
        },
        "code_checks": {"quotes": [{"hypothesis_ref": "p", "kept": True}],
                        "gates": [{"gate": name, "result": "no_change"}
                                  for name in ("comove", "subject", "timing")]},
    }]}

    assert derive_abstention_reason(no_news) == {
        "reason_code": "NO_STOCK_NEWS", "details": {},
    }
    assert derive_abstention_reason(already_abstained) == {
        "reason_code": "ALREADY_ABSTAIN", "details": {},
    }


def test_abstention_reason_text_is_fixed_for_every_code():
    expected = {
        "NO_STOCK_NEWS": "No in-window evidence about this stock was available to the analyst.",
        "NO_PRIMARY_PROPOSED": "No primary cause was proposed for publication.",
        "PRIMARY_QUOTES_FAILED": "The proposed primary cause had no quote that passed the quote checks.",
        "GATE_FAILED": "A required attribution gate blocked the proposed cause.",
        "VERIFIER_REJECTED": "The verifier rejected the proposed primary cause.",
        "VERIFIER_NO_VERDICT": "The verifier did not return a usable verdict for the primary cause.",
        "CEILING_ABSTAIN": "The evidence and policy checks limited the attribution to an abstention.",
        "ALREADY_ABSTAIN": "The analyst abstained before a cause could be verified.",
        "STALE_PRIMARY": "The leading candidate event happened before this session's window (prior close to close).",
    }
    assert set(ABSTENTION_REASON_CODES) == set(expected)
    for code, text in expected.items():
        assert abstention_reason_text(code) == text
