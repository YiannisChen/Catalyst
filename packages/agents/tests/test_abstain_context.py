"""An abstention shows a labeled, unestablished explanation only for UNLINKED rejections."""
from types import SimpleNamespace as NS

from catalyst_agents.attribution.abstain_context import possible_explanation, structured


def _verifier(verdict, category):
    return NS(last={"statement": "“Quote.” — Benzinga, 2026-03-03 15:05 UTC, https://x",
                    "verdict": verdict, "category": category, "reason": "no report ties it to the move"})


def test_only_unlinked_rejections_are_shown():
    text = possible_explanation(_verifier(False, "UNLINKED"))
    assert text.startswith("“Quote.”") and "Not established: no report ties it to the move" in text
    for category in ("RECAP", "PREVIEW", "EVERGREEN", "OTHER_COMPANY", "AGAINST_GROUP"):
        assert possible_explanation(_verifier(False, category)) == ""
    assert possible_explanation(_verifier(True, "COMPANY_NEWS")) == ""
    assert possible_explanation(NS(last=None)) == "" and possible_explanation(None) == ""


def test_possible_explanation_filters_other_company_session_and_preview_events():
    quote = {"text": "HONA shares plunged after earnings.", "roles": ["EVENT"],
             "role_reasons": {"EVENT": ["EVENT_CONCRETE", "EVENT_SUBJECT_MISMATCH"]}}
    pack = NS(evidence_inventory=(), temporal_identity=NS(publication_window=lambda: (None, None)))
    assert structured(_verifier(False, "UNLINKED"), pack, "HON", [quote])["possible_explanation"] is None
    for category in ("OTHER_COMPANY", "OTHER_SESSION", "PREVIEW"):
        result = structured(_verifier(False, category), pack, "HON", [quote])
        assert result["possible_explanation"] is None
    on_target = {**quote, "role_reasons": {"EVENT": ["EVENT_CONCRETE", "EVENT_SUBJECT_MATCH"]}}
    assert structured(_verifier(False, "UNLINKED"), pack, "HON", [on_target])["possible_explanation"]


def test_structured_explanation_only_for_exact_unlinked_verifier_rejection():
    quote = {"text": "HONA shares plunged after earnings.", "roles": ["EVENT"],
             "role_reasons": {"EVENT": ["EVENT_CONCRETE", "EVENT_SUBJECT_MATCH"]}}
    pack = NS(evidence_inventory=(), temporal_identity=NS(publication_window=lambda: (None, None)))
    assert structured(_verifier(True, "UNLINKED"), pack, "HON", [quote])["possible_explanation"] is None
    assert structured(_verifier(False, "DUPLICATE"), pack, "HON", [quote])["possible_explanation"] is None
    assert structured(_verifier(False, "CONTRADICTORY"), pack, "HON", [quote])["possible_explanation"] is None
    assert structured(_verifier(False, "COMPANY_NEWS"), pack, "HON", [quote])["possible_explanation"] is None
    assert structured(NS(last={"verdict": False, "category": "OTHER"}), pack, "HON", [quote])["possible_explanation"] is None
    assert structured(NS(last=None), pack, "HON", [quote])["possible_explanation"] is None
    assert structured(None, pack, "HON", [quote])["possible_explanation"] is None
    result = structured(_verifier(False, "UNLINKED"), pack, "HON", [quote])["possible_explanation"]
    assert result["quotes"] == [quote]
    assert result["verifier_reason"] == "No attribution quote met the event-time grade requirements."
