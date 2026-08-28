"""M7-1: V1.1 GoldenCase loader + dataset provenance.

The loader is eval-owned and never imported by production packages. It
validates the full Frozen V1.1 case contract, rejects legacy-only values
(INSUFFICIENT), enforces ABSTAIN refusal reasons, rejects
MARKET_STRUCTURE_UNSUPPORTED outside gap reason codes, and verifies the
dataset content hash against a manifest.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from catalyst_eval.v1_1.case import (
    AttributionTypeV1,
    CauseTypeV1,
    ExpectedResearchBehavior,
    GoldenCase,
    GoldenCaseLineage,
    OracleStatusV1,
)
from catalyst_eval.v1_1.loader import (
    dataset_content_sha256,
    dataset_provenance,
    load_golden_cases,
)

MANIFEST_SCHEMA = "v1_1_stage1_dataset_manifest_v1"


def _case_dict(
    *,
    case_id: str = "g006",
    oracle_status: str = "SUFFICIENT",
    expected_refusal_reason: str | None = None,
    question: str = "Why did TSLA move after hours on July 24?",
    expected_attribution_type: str | None = "EVIDENCE_BACKED_CAUSAL",
    cause_types: tuple[str, ...] = ("COMPANY_SPECIFIC_CATALYST",),
) -> dict:
    return {
        "case_id": case_id,
        "ticker": "TSLA",
        "session_date": "2025-07-24",
        "cutoff": "2025-07-24T20:00:00Z",
        "question": question,
        "oracle_status": oracle_status,
        "acceptable_cause_labels": [
            {
                "cause_type": cause_type,
                "label": f"label-{cause_type}",
                "direction": "negative",
                "materiality": "material",
            }
            for cause_type in cause_types
        ],
        "evidence_judgments": [
            {
                "evidence_id": "tsla-c1",
                "canonical_asset_id": "asset:tsla",
                "canonical_content_version_id": "ver:1",
                "chunk_id": "tsla-c1",
                "fact_id": None,
                "role": "primary_support",
                "support": True,
                "materiality": "material",
                "temporal_eligible": True,
                "independence_group": "grp-1",
                "rationale": "primary earnings evidence",
                "annotator": "reviewer-1",
                "annotated_at": "2026-08-19T00:00:00Z",
            }
        ],
        "expected_primary_evidence": ["tsla-c1"],
        "expected_refusal_reason": expected_refusal_reason,
        "expected_attribution_type": expected_attribution_type,
        "expected_research_behavior": {
            "acceptable_initial_tasks": ["COMPANY_PRIMARY"],
            "expected_gap_reason_codes": [],
            "acceptable_corrective_actions": [],
            "corrective_recoverable": False,
            "corrective_required": False,
        },
        "notes": None,
        "dataset_version": "1.1.0",
        "lineage": {
            "source": "legacy:T4",
            "model_assisted_fields": [],
            "human_confirmed_fields": ["oracle_status", "evidence_judgments"],
            "annotated_at": "2026-08-19T00:00:00Z",
            "adjudication_state": "resolved",
        },
    }


def _write_cases(tmp_path, rows: list[dict]) -> Path:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _manifest_for(cases: list[GoldenCase], *, dataset_id: str = "stage1-fixture") -> dict:
    return {
        "schema_version": MANIFEST_SCHEMA,
        "dataset_id": dataset_id,
        "dataset_version": "1.1.0",
        "ordered_case_ids": [case.case_id for case in cases],
        "case_list_sha256": "0" * 64,
        "dataset_content_sha256": dataset_content_sha256(cases),
        "case_count": len(cases),
        "adjudication_state": "resolved",
    }


def test_loader_reads_valid_golden_cases(tmp_path):
    path = _write_cases(tmp_path, [_case_dict()])
    cases = load_golden_cases(path)
    assert len(cases) == 1
    assert isinstance(cases[0], GoldenCase)
    assert cases[0].case_id == "g006"
    assert cases[0].oracle_status == "SUFFICIENT"


def test_loader_accepts_abstain_with_refusal_reason(tmp_path):
    path = _write_cases(
        tmp_path,
        [_case_dict(case_id="h004", oracle_status="ABSTAIN",
                    expected_refusal_reason="insufficient_public_evidence",
                    expected_attribution_type=None)],
    )
    cases = load_golden_cases(path)
    assert cases[0].expected_refusal_reason == "insufficient_public_evidence"


def test_loader_rejects_insufficient_oracle_status(tmp_path):
    path = _write_cases(tmp_path, [_case_dict(oracle_status="INSUFFICIENT")])
    with pytest.raises(ValueError, match="INSUFFICIENT"):
        load_golden_cases(path)


def test_loader_rejects_abstain_without_refusal_reason(tmp_path):
    path = _write_cases(
        tmp_path,
        [_case_dict(case_id="h004", oracle_status="ABSTAIN",
                    expected_refusal_reason=None, expected_attribution_type=None)],
    )
    with pytest.raises(ValueError, match="refusal"):
        load_golden_cases(path)


def test_loader_rejects_market_structure_as_cause_type(tmp_path):
    path = _write_cases(
        tmp_path,
        [_case_dict(cause_types=("MARKET_STRUCTURE_UNSUPPORTED",))],
    )
    with pytest.raises(ValueError, match="MARKET_STRUCTURE_UNSUPPORTED"):
        load_golden_cases(path)


def test_loader_rejects_market_structure_as_attribution_type(tmp_path):
    path = _write_cases(
        tmp_path,
        [_case_dict(expected_attribution_type="MARKET_STRUCTURE_UNSUPPORTED")],
    )
    with pytest.raises(ValueError, match="MARKET_STRUCTURE_UNSUPPORTED"):
        load_golden_cases(path)


def test_loader_rejects_duplicate_case_ids(tmp_path):
    path = _write_cases(tmp_path, [_case_dict(), _case_dict()])
    with pytest.raises(ValueError, match="duplicate"):
        load_golden_cases(path)


def test_loader_verifies_dataset_content_hash_against_manifest(tmp_path):
    rows = [_case_dict(), _case_dict(case_id="g007")]
    path = _write_cases(tmp_path, rows)
    cases = load_golden_cases(path)
    manifest = _manifest_for(cases)
    reloaded = load_golden_cases(path, manifest=manifest)
    assert [c.case_id for c in reloaded] == ["g006", "g007"]


def test_loader_rejects_case_hash_mismatch_with_manifest(tmp_path):
    rows = [_case_dict(), _case_dict(case_id="g007")]
    path = _write_cases(tmp_path, rows)
    cases = load_golden_cases(path)
    manifest = _manifest_for(cases)
    # Mutate the file after the manifest was computed.
    rows[1]["question"] = "Why did TSLA move differently?"
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="content hash"):
        load_golden_cases(path, manifest=manifest)


def test_content_hash_is_deterministic_and_order_sensitive():
    a = GoldenCase.model_validate(_case_dict())
    b = GoldenCase.model_validate(_case_dict(case_id="g007"))
    assert dataset_content_sha256([a, b]) == dataset_content_sha256([a, b])
    assert dataset_content_sha256([a, b]) != dataset_content_sha256([b, a])


def test_content_hash_changes_when_case_content_changes():
    a = GoldenCase.model_validate(_case_dict())
    b = GoldenCase.model_validate(
        _case_dict(case_id="g007", question="Why did TSLA move differently?")
    )
    assert dataset_content_sha256([a]) != dataset_content_sha256([b])


def test_dataset_provenance_contains_hash_and_lineage(tmp_path):
    path = _write_cases(tmp_path, [_case_dict()])
    cases = load_golden_cases(path)
    manifest = _manifest_for(cases)
    provenance = dataset_provenance(manifest, cases)
    assert dataset_content_sha256(cases) in provenance
    assert manifest["dataset_id"] in provenance
    assert "legacy:T4" in provenance
    # Deterministic for identical inputs.
    assert provenance == dataset_provenance(manifest, cases)
