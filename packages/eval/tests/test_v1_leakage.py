"""M7-4: hidden-gold isolation and leakage scan.

Forbidden oracle fields (statuses, cause labels, expected research behavior,
reviewer notes, relevance/materiality/independence annotations, expected
refusal, hidden answer text) must never appear in production prompts, state,
traces, or model-visible artifacts. Evidence ids arriving through the served
retrieval result/ContextPack inventory are allowed and scored later; an
evidence id is a leak only when copied from hidden gold without a matching
served record. Findings carry field/path/reason but never repeat secret or
hidden-gold values.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from catalyst_eval.v1_1.case import GoldenCase
from catalyst_eval.v1_1.leakage import (
    RunArtifactInput,
    scan_run_for_gold,
    verify_hidden_gold_boundary,
)

from tests.v1_1_fixtures import make_case


def _gold(**overrides) -> GoldenCase:
    row = make_case(
        case_id="v1f-001",
        ticker="TSLA",
        session_date="2025-07-24",
        cutoff="2025-07-24T20:00:00Z",
        question="Why did TSLA fall after earnings?",
        oracle_status="SUFFICIENT",
        direction="negative",
        cause_types=("COMPANY_SPECIFIC_CATALYST",),
        labels=("hidden-gold-label-alpha",),
        evidence_ids=("fixture-ev-001",),
        expected_primary_evidence=("fixture-ev-001",),
        notes="reviewer hidden note",
    )
    row.update(overrides)
    return GoldenCase.model_validate(row)


def _artifact(path: str, payload, *, kind: str = "prompt") -> RunArtifactInput:
    return RunArtifactInput(path=path, kind=kind, payload=payload)


def test_clean_prompt_with_served_evidence_passes():
    gold = _gold()
    artifacts = [
        _artifact(
            "prompt:analyst",
            {"system": "Answer the user question using served evidence only."},
        ),
        _artifact(
            "trace:retrieval",
            {"served_evidence_ids": ["fixture-ev-001"]},
            kind="trace",
        ),
        _artifact(
            "output:final",
            {"status": "SUFFICIENT"},
            kind="output",
        ),
    ]
    findings = scan_run_for_gold(artifacts, gold, served_evidence_ids=("fixture-ev-001",))
    assert findings == []


def test_oracle_status_in_prompt_is_flagged():
    gold = _gold()
    findings = scan_run_for_gold(
        [_artifact("prompt:analyst", {"text": "expected status SUFFICIENT"})],
        gold,
    )
    assert findings
    assert any("oracle status" in f for f in findings)
    assert any("prompt:analyst" in f for f in findings)
    # Findings never repeat the hidden value.
    assert all("SUFFICIENT" not in f for f in findings)


def test_cause_label_in_prompt_is_flagged():
    gold = _gold()
    findings = scan_run_for_gold(
        [_artifact("prompt:analyst", {"text": "the hidden-gold-label-alpha driver"})],
        gold,
    )
    assert findings
    assert any("cause label" in f for f in findings)
    assert all("hidden-gold-label-alpha" not in f for f in findings)


def test_expected_research_behavior_field_is_flagged():
    gold = _gold()
    findings = scan_run_for_gold(
        [
            _artifact(
                "state:research_policy",
                {"expected_research_behavior": {"corrective_required": True}},
                kind="state",
            )
        ],
        gold,
    )
    assert findings
    assert any("expected_research_behavior" in f for f in findings)


def test_reviewer_notes_and_annotations_are_flagged():
    gold = _gold()
    findings = scan_run_for_gold(
        [
            _artifact("state:notes", {"notes": "reviewer hidden note"}),
            _artifact(
                "artifact:evidence",
                {"independence_group": "grp-0", "materiality": "material"},
                kind="artifact",
            ),
        ],
        gold,
    )
    assert findings
    assert any("notes" in f for f in findings)
    assert any("independence_group" in f for f in findings)
    assert any("materiality" in f for f in findings)


def test_expected_refusal_in_prompt_is_flagged():
    gold = _gold(expected_refusal_reason="insufficient_public_evidence")
    findings = scan_run_for_gold(
        [_artifact("prompt:analyst", {"text": "refuse: insufficient_public_evidence"})],
        gold,
    )
    assert findings
    assert any("refusal" in f for f in findings)


def test_gold_evidence_id_without_served_record_is_flagged():
    gold = _gold()
    findings = scan_run_for_gold(
        [_artifact("state:context", {"evidence_ids": ["fixture-ev-001"]})],
        gold,
    )
    assert findings
    assert any("evidence id" in f for f in findings)


def test_gold_evidence_id_with_matching_served_record_is_allowed():
    gold = _gold()
    findings = scan_run_for_gold(
        [_artifact("state:context", {"evidence_ids": ["fixture-ev-001"]})],
        gold,
        served_evidence_ids=("fixture-ev-001",),
    )
    assert findings == []


def test_output_status_is_not_flagged_as_oracle():
    gold = _gold()
    findings = scan_run_for_gold(
        [_artifact("output:final", {"status": "SUFFICIENT"}, kind="output")],
        gold,
    )
    assert findings == []


def test_hidden_answer_text_in_notes_is_flagged():
    gold = _gold(notes="the hidden answer is hidden-gold-label-alpha")
    findings = scan_run_for_gold(
        [_artifact("artifact:answer", {"text": "hidden-gold-label-alpha"})],
        gold,
    )
    assert findings


def test_verify_hidden_gold_boundary_repo_is_clean():
    violations = verify_hidden_gold_boundary()
    assert violations == []


def test_verify_hidden_gold_boundary_detects_forbidden_import(tmp_path, monkeypatch):
    fake_agents = tmp_path / "agents" / "catalyst_agents"
    fake_agents.mkdir(parents=True)
    (fake_agents / "runtime.py").write_text(
        "from catalyst_eval.golden_set import cases\n"
    )
    monkeypatch.setattr(
        "catalyst_eval.v1_1.leakage._PACKAGE_ROOTS",
        (tmp_path / "agents",),
    )
    violations = verify_hidden_gold_boundary()
    assert violations
    assert any("forbidden gold import" in v for v in violations)
