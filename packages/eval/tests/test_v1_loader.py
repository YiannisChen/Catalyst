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
    BenchmarkDatasetManifest,
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


def test_benchmark_manifest_rejects_execution_ceiling_keys():
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "dataset_id": "stage1-fixture",
        "dataset_version": "1.1.0",
        "case_count": 1,
        "ordered_case_ids": ("g006",),
        "case_list_sha256": "0" * 64,
        "dataset_content_sha256": "1" * 64,
        "reviewer_ids": ("reviewer-1",),
        "max_provider_calls": 10,
    }
    with pytest.raises(ValueError, match="ceiling|execution"):
        BenchmarkDatasetManifest.from_mapping(manifest)


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


# ---------------------------------------------------------------------------
# Phase A: benchmark naming resolution + incremental large-file hashing
# ---------------------------------------------------------------------------

def test_resolve_cases_prefers_benchmark_name_then_legacy(tmp_path):
    from catalyst_eval.v1_1.loader import (
        resolve_benchmark_cases_path,
        resolve_benchmark_stratification_path,
    )

    bench = tmp_path / "benchmarks" / "v1_1" / "stage1"
    bench.mkdir(parents=True)
    (bench / "cases.jsonl").write_text("{}\n", encoding="utf-8")
    (bench / "manifest.json").write_text("{}", encoding="utf-8")
    (bench / "stratification.json").write_text("{}", encoding="utf-8")
    manifest_path = bench / "manifest.json"
    assert resolve_benchmark_cases_path({}, manifest_path) == bench / "cases.jsonl"
    assert (
        resolve_benchmark_stratification_path({}, manifest_path)
        == bench / "stratification.json"
    )

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "v1_1_stage1_cases.jsonl").write_text("{}\n", encoding="utf-8")
    legacy_manifest = legacy / "v1_1_stage1_dataset_manifest.json"
    legacy_manifest.write_text("{}", encoding="utf-8")
    assert (
        resolve_benchmark_cases_path({}, legacy_manifest)
        == legacy / "v1_1_stage1_cases.jsonl"
    )
    # No stratification sibling file -> None, never fabricated.
    assert resolve_benchmark_stratification_path({}, legacy_manifest) is None


def test_streamed_sha256_matches_read_bytes_and_is_chunked(tmp_path):
    import hashlib

    from catalyst_eval.v1_1.loader import (
        RUNTIME_DB_HASH_CHUNK_BYTES,
        streamed_sha256,
    )

    payload = b"runtime-authority-chunk\x00" * (RUNTIME_DB_HASH_CHUNK_BYTES // 4)
    target = tmp_path / "runtime.db"
    target.write_bytes(payload)
    assert streamed_sha256(target) == hashlib.sha256(payload).hexdigest()


def test_benchmark_schema_and_legacy_schema_both_accepted():
    from catalyst_eval.v1_1.loader import (
        BENCHMARK_MANIFEST_SCHEMA,
        STAGE1_MANIFEST_SCHEMA,
        STAGE1_MANIFEST_SCHEMAS,
    )

    assert BENCHMARK_MANIFEST_SCHEMA in STAGE1_MANIFEST_SCHEMAS
    assert STAGE1_MANIFEST_SCHEMA in STAGE1_MANIFEST_SCHEMAS


def test_benchmark_public_contract_aliases_are_available():
    from catalyst_eval.v1_1.case import BenchmarkCase, GoldenCase
    from catalyst_eval.v1_1.loader import BENCHMARK_DATASET_FILE_NAMES
    from catalyst_eval.v1_1.output_audit import (
        HumanOutputAudit,
        Stage1OutputAudit,
    )

    assert BenchmarkCase is GoldenCase
    assert HumanOutputAudit is Stage1OutputAudit
    assert BENCHMARK_DATASET_FILE_NAMES == (
        "cases.jsonl",
        "stratification.json",
        "manifest.json",
    )


def test_write_benchmark_dataset_files_is_write_once_and_rejects_legacy_schema(
    tmp_path,
):
    import copy

    from tests.v1_1_fixtures import (
        make_dataset_manifest,
        make_stage1_cases,
        make_stratification,
    )
    from catalyst_eval.v1_1.loader import (
        BENCHMARK_MANIFEST_SCHEMA,
        PublicationConflictError,
        write_benchmark_dataset_files,
    )

    rows = make_stage1_cases()
    stratification = make_stratification(rows)
    manifest = make_dataset_manifest(rows, stratification)
    manifest["schema_version"] = BENCHMARK_MANIFEST_SCHEMA
    manifest["data_runtime_identity_ref"] = "v1:corpus:q011"
    manifest["data_runtime_identity_hash"] = "a" * 64

    with pytest.raises(ValueError, match="ceiling"):
        write_benchmark_dataset_files(
            tmp_path / "ceiling-rejected",
            rows=rows,
            stratification=stratification,
            manifest={**manifest, "max_cost_usd": 1.0},
        )

    out = tmp_path / "stage1"
    digests = write_benchmark_dataset_files(
        out, rows=rows, stratification=stratification, manifest=manifest
    )
    assert set(digests) == {"cases.jsonl", "stratification.json", "manifest.json"}
    # Idempotent re-write of identical bytes.
    assert write_benchmark_dataset_files(
        out, rows=rows, stratification=stratification, manifest=manifest
    ) == digests
    # A differing dataset conflicts rather than overwriting.
    changed = copy.deepcopy(rows)
    changed[0]["notes"] = "changed"
    from catalyst_eval.v1_1.case import GoldenCase
    from catalyst_eval.v1_1.loader import dataset_content_sha256

    changed_cases = [GoldenCase.model_validate(r) for r in changed]
    changed_manifest = dict(manifest)
    changed_manifest["dataset_content_sha256"] = dataset_content_sha256(changed_cases)
    with pytest.raises(PublicationConflictError):
        write_benchmark_dataset_files(
            out,
            rows=changed,
            stratification=stratification,
            manifest=changed_manifest,
        )

    legacy_manifest = dict(manifest)
    legacy_manifest["schema_version"] = "v1_1_stage1_dataset_manifest_v1"
    with pytest.raises(ValueError, match="benchmark manifest schema"):
        write_benchmark_dataset_files(
            tmp_path / "other",
            rows=rows,
            stratification=stratification,
            manifest=legacy_manifest,
        )


# ---------------------------------------------------------------------------
# Phase A: public benchmark naming surfaces + publication boundary
# ---------------------------------------------------------------------------

def test_benchmark_dataset_manifest_contract_validates_and_preserves_extras():
    from catalyst_eval.v1_1.loader import BenchmarkDatasetManifest
    from tests.v1_1_fixtures import (
        make_dataset_manifest,
        make_stage1_cases,
        make_stratification,
    )

    rows = make_stage1_cases()
    raw = make_dataset_manifest(rows, make_stratification(rows))
    manifest = BenchmarkDatasetManifest.from_mapping(raw)
    assert manifest.case_count == 12
    assert manifest.ordered_case_ids == tuple(raw["ordered_case_ids"])
    # Envelope keys outside the typed contract are preserved verbatim.
    assert manifest.to_mapping()["approval_authority"] == raw["approval_authority"]

    import pytest

    with pytest.raises(Exception):
        BenchmarkDatasetManifest.from_mapping(
            {**raw, "schema_version": "v1_1_unknown_manifest_v9"}
        )
    bad = dict(raw)
    bad.pop("case_count")
    with pytest.raises(Exception):
        BenchmarkDatasetManifest.from_mapping(bad)


def test_load_benchmark_cases_loads_benchmark_named_jsonl(tmp_path):
    import json as _json

    from catalyst_eval.v1_1.loader import load_benchmark_cases
    from tests.v1_1_fixtures import make_stage1_cases

    rows = make_stage1_cases()
    path = tmp_path / "cases.jsonl"
    path.write_text(
        "".join(_json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    cases = load_benchmark_cases(path)
    assert [case.case_id for case in cases] == [row["case_id"] for row in rows]


def test_v1_1_public_exports_expose_benchmark_surfaces():
    import catalyst_eval.v1_1 as v1_1
    from catalyst_eval.v1_1.case import GoldenCase
    from catalyst_eval.v1_1.output_audit import Stage1OutputAudit

    assert v1_1.BenchmarkCase is GoldenCase
    assert v1_1.HumanOutputAudit is Stage1OutputAudit
    assert hasattr(v1_1, "BenchmarkDatasetManifest")
    assert callable(v1_1.load_benchmark_cases)


def test_stage1_benchmark_publication_is_complete_and_budget_free():
    """The Gate-1 publication contains all canonical files and no ceilings."""
    import json
    from pathlib import Path

    bench_dir = Path(__file__).resolve().parents[1] / "benchmarks" / "v1_1" / "stage1"
    readme = bench_dir / "README.md"
    assert readme.is_file(), "benchmark README is missing"
    text = " ".join(readme.read_text(encoding="utf-8").lower().split())
    assert "no provider-call ceiling" in text
    assert "no usd cost ceiling" in text
    assert "status: published" in text
    for name in ("cases.jsonl", "stratification.json", "manifest.json"):
        assert (bench_dir / name).is_file(), f"published benchmark is missing {name}"
    manifest = json.loads((bench_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "v1_1_benchmark_dataset_manifest_v1"
    assert manifest["case_count"] == 12
    assert "max_provider_calls" not in manifest
    assert "max_cost_usd" not in manifest
