"""AMEND-2 P1: full fail-closed T4 evidence validator.

``validate_t4_evidence()`` must return a non-trivial ``ValidatedT4Evidence``
object (never a caller-forgeable string). Every rejection must happen before
model factory, embedding, retrieval, staging directory, and artifact writes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from catalyst_eval.post_import.case_pack import (
    build_smoke_case_pack,
    compute_case_pack_id,
    write_case_pack,
)
from catalyst_eval.post_import.index_identity import ResolvedRuntimeIdentity
from catalyst_eval.post_import.probe import (
    ServedCorpusProbeReport,
    CaseProbeResult,
    write_probe_evidence,
)
from catalyst_eval.post_import.t4_evidence import (
    ValidatedT4Evidence,
    validate_t4_evidence,
)

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden_set"

GIT_HEAD = "8dd9ee9b5f04e848e3d8248dad6470189af79573"
CODE_REVISION = "bb43ebe20f29a13ef426e0a1a7c3aefc6d15ffd8"

EVIDENCE_KWARGS = {
    "db_sha256": "bb37b213091e256033fa00272cb7a85617dbcddf69d6fe9b515840cd9f1ebe40",
    "corpus_manifest_id": "3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc",
    "snapshot_id": "7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49",
    "source_bundle_id": "8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2",
    "probe_report_id": "25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23",
    "postbuild_readiness_id": "9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b",
    "index_manifest_id": "c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083",
    "db_path": "data/snapshots/catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db",
    "db_user_version": 13,
    "db_foreign_key_violations": 0,
    "lancedb_dir": "data/lancedb_gold/b6g_8ffae891b4e1",
    "active_table_name": "chunks__staging__b3761f4b943542a8",
    "model_name": "BAAI/bge-m3",
    "model_revision": "5617a9f61b028005a4858fdac845db406aefb181",
    "tokenizer_revision": "5617a9f61b028005a4858fdac845db406aefb181",
    "dimension": 1024,
    "dtype": "float32",
    "normalization_mode": "l2",
    "embedding_mode": "mock_unit_test",
}


def _resolved(**overrides) -> ResolvedRuntimeIdentity:
    values = {
        "lancedb_dir": Path("data/lancedb_gold/b6g_8ffae891b4e1"),
        "active_table_name": "chunks__staging__b3761f4b943542a8",
        "snapshot_id": EVIDENCE_KWARGS["snapshot_id"],
        "corpus_manifest_id": EVIDENCE_KWARGS["corpus_manifest_id"],
        "source_bundle_id": EVIDENCE_KWARGS["source_bundle_id"],
        "index_manifest_id": EVIDENCE_KWARGS["index_manifest_id"],
        "probe_report_id": EVIDENCE_KWARGS["probe_report_id"],
        "postbuild_readiness_id": EVIDENCE_KWARGS["postbuild_readiness_id"],
        "code_revision": CODE_REVISION,
        "git_head": GIT_HEAD,
        "model_name": "BAAI/bge-m3",
        "model_revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "tokenizer_revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "dimension": 1024,
        "dtype": "float32",
        "normalization_mode": "l2",
        "vector_count": 295506,
        "db_path": Path("data/snapshots/catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db"),
        "db_sha256": EVIDENCE_KWARGS["db_sha256"],
        "db_user_version": 13,
        "db_foreign_key_violations": 0,
        "lancedb_row_count": 295506,
    }
    values.update(overrides)
    return ResolvedRuntimeIdentity(**values)


def _build_evidence(
    tmp_path: Path,
    *,
    cases=None,
    report=None,
    case_pack_id: str | None = None,
    resolved: ResolvedRuntimeIdentity | None = None,
) -> tuple[Path, list, ResolvedRuntimeIdentity]:
    cases = cases if cases is not None else build_smoke_case_pack(GOLDEN_DIR)
    resolved = resolved if resolved is not None else _resolved()
    if case_pack_id is None:
        case_pack_id = compute_case_pack_id(cases)
    if report is None:
        report = ServedCorpusProbeReport(
            schema_version="served_corpus_probe_v1",
            corpus_manifest_id=EVIDENCE_KWARGS["corpus_manifest_id"],
            case_count=len(cases),
            passed_count=len(cases),
            all_passed=True,
            per_case=tuple(
                CaseProbeResult(c.case_id, c.ticker, c.cutoff, 1) for c in cases
            ),
        )
    evidence_dir = tmp_path / "evidence"
    write_case_pack(cases, evidence_dir / "case_pack.jsonl")
    write_probe_evidence(
        report,
        run_dir=evidence_dir,
        case_pack_id=case_pack_id,
        case_pack_path="case_pack.jsonl",
        runtime_git_head=GIT_HEAD,
        index_build_code_revision=CODE_REVISION,
        **{k: v for k, v in EVIDENCE_KWARGS.items() if k not in {"case_pack_id", "case_pack_path"}},
    )
    return evidence_dir, cases, resolved


def test_valid_evidence_returns_validated_object(tmp_path):
    evidence_dir, cases, resolved = _build_evidence(tmp_path)
    validated = validate_t4_evidence(
        evidence_dir=evidence_dir,
        current_case_pack=cases,
        resolved=resolved,
    )
    assert isinstance(validated, ValidatedT4Evidence)
    assert validated.case_pack_id == compute_case_pack_id(cases)
    assert validated.case_count == 10
    assert validated.passed_count == 10
    assert validated.active_table_name == "chunks__staging__b3761f4b943542a8"
    assert validated.corpus_manifest_id == EVIDENCE_KWARGS["corpus_manifest_id"]


def test_fake_evidence_with_only_meta_and_case_pack_id_is_rejected(tmp_path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "meta.json").write_text(json.dumps({"case_pack_id": "a" * 64}))
    with pytest.raises(ValueError, match="T4_PROBE_TOKEN"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=None, resolved=None)


def test_missing_t4_token_rejected(tmp_path):
    evidence_dir, cases, resolved = _build_evidence(tmp_path)
    (evidence_dir / "T4_PROBE_TOKEN.txt").unlink()
    with pytest.raises(ValueError, match="T4_PROBE_TOKEN"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved)


def test_wrong_t4_token_rejected(tmp_path):
    evidence_dir, cases, resolved = _build_evidence(tmp_path)
    (evidence_dir / "T4_PROBE_TOKEN.txt").write_text("WRONG_TOKEN\n")
    with pytest.raises(ValueError, match="T4_PROBE_OK"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved)


def test_wave_token_present_rejected(tmp_path):
    evidence_dir, cases, resolved = _build_evidence(tmp_path)
    (evidence_dir / "WAVE_TOKEN.txt").write_text("FOUR_ARM_E2E_OK\n")
    with pytest.raises(ValueError, match="WAVE_TOKEN"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved)


@pytest.mark.parametrize("filename", ["probe_report.json", "case_pack.jsonl", "meta.json"])
def test_missing_required_file_rejected(tmp_path, filename):
    evidence_dir, cases, resolved = _build_evidence(tmp_path)
    (evidence_dir / filename).unlink()
    with pytest.raises(ValueError, match="missing"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved)


def test_probe_hash_mismatch_rejected(tmp_path):
    evidence_dir, cases, resolved = _build_evidence(tmp_path)
    probe = json.loads((evidence_dir / "probe_report.json").read_text())
    probe["per_case"][0]["count"] = 99
    (evidence_dir / "probe_report.json").write_text(json.dumps(probe, sort_keys=True))
    with pytest.raises(ValueError, match="probe_report_sha256|hash"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved)


def test_probe_all_passed_false_rejected(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    evidence_dir = _write_manual_evidence(
        tmp_path / "evidence",
        cases,
        {
            "schema_version": "served_corpus_probe_v1",
            "corpus_manifest_id": EVIDENCE_KWARGS["corpus_manifest_id"],
            "case_count": 10,
            "passed_count": 9,
            "all_passed": False,
            "per_case": [CaseProbeResult(c.case_id, c.ticker, c.cutoff, 1 if i else 0).to_dict()
                         for i, c in enumerate(cases)],
        },
    )
    with pytest.raises(ValueError, match="all_passed|passed"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=_resolved())


def test_probe_case_count_9_of_10_rejected(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    report = ServedCorpusProbeReport(
        schema_version="served_corpus_probe_v1",
        corpus_manifest_id=EVIDENCE_KWARGS["corpus_manifest_id"],
        case_count=9,
        passed_count=9,
        all_passed=True,
        per_case=tuple(CaseProbeResult(c.case_id, c.ticker, c.cutoff, 1) for c in cases[:9]),
    )
    evidence_dir, _, resolved = _build_evidence(tmp_path, cases=cases, report=report)
    with pytest.raises(ValueError, match="case_count|10"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved)


def test_probe_zero_count_rejected(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    evidence_dir = _write_manual_evidence(
        tmp_path / "evidence",
        cases,
        {
            "schema_version": "served_corpus_probe_v1",
            "corpus_manifest_id": EVIDENCE_KWARGS["corpus_manifest_id"],
            "case_count": 10,
            "passed_count": 10,
            "all_passed": True,
            "per_case": [CaseProbeResult(c.case_id, c.ticker, c.cutoff, 0 if i == 0 else 1).to_dict()
                         for i, c in enumerate(cases)],
        },
    )
    with pytest.raises(ValueError, match="count"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=_resolved())


def test_probe_per_case_mismatch_rejected(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    evidence_dir = _write_manual_evidence(
        tmp_path / "evidence",
        cases,
        {
            "schema_version": "served_corpus_probe_v1",
            "corpus_manifest_id": EVIDENCE_KWARGS["corpus_manifest_id"],
            "case_count": 10,
            "passed_count": 10,
            "all_passed": True,
            "per_case": [CaseProbeResult("WRONG_ID", c.ticker, c.cutoff, 1).to_dict() for c in cases],
        },
    )
    with pytest.raises(ValueError, match="case"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=_resolved())


def test_t4_meta_identity_mismatch_rejected(tmp_path):
    evidence_dir, cases, resolved = _build_evidence(tmp_path)
    meta = json.loads((evidence_dir / "meta.json").read_text())
    meta["corpus_manifest_id"] = "e" * 64
    (evidence_dir / "meta.json").write_text(json.dumps(meta, sort_keys=True))
    with pytest.raises(ValueError, match="corpus_manifest_id"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved)


def test_current_case_pack_mismatch_rejected(tmp_path):
    evidence_dir, cases, resolved = _build_evidence(tmp_path)
    other_cases = [c for c in build_smoke_case_pack(GOLDEN_DIR) if c.case_id != "g006"]
    with pytest.raises(ValueError, match="case pack|case_pack"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=other_cases, resolved=resolved)


def test_non_hex_case_pack_id_rejected(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    evidence_dir, _, resolved = _build_evidence(tmp_path, cases=cases, case_pack_id="Z" * 64)
    with pytest.raises(ValueError, match="hex|case_pack_id"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved)


def test_upper_case_hex_case_pack_id_rejected(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    upper = compute_case_pack_id(cases).upper()
    evidence_dir, _, resolved = _build_evidence(tmp_path, cases=cases, case_pack_id=upper)
    with pytest.raises(ValueError, match="lowercase|hex"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved)




def _write_manual_evidence(evidence_dir: Path, cases, probe_body: dict, meta_extra: dict | None = None) -> Path:
    """Write T4 evidence files with an arbitrary (possibly failing) probe body."""
    import hashlib as _hl

    evidence_dir.mkdir(parents=True, exist_ok=True)
    write_case_pack(cases, evidence_dir / "case_pack.jsonl")
    (evidence_dir / "probe_report.json").write_text(json.dumps(probe_body, sort_keys=True))
    probe_sha = _hl.sha256((evidence_dir / "probe_report.json").read_bytes()).hexdigest()
    meta = {
        "schema_version": "t4_probe_meta_v1",
        "task": "T4",
        "phase": "wave2_preparation",
        "runtime_git_head": GIT_HEAD,
        "index_build_code_revision": CODE_REVISION,
        "db_sha256": EVIDENCE_KWARGS["db_sha256"],
        "db_user_version": 13,
        "db_foreign_key_violations": 0,
        "snapshot_id": EVIDENCE_KWARGS["snapshot_id"],
        "corpus_manifest_id": EVIDENCE_KWARGS["corpus_manifest_id"],
        "source_bundle_id": EVIDENCE_KWARGS["source_bundle_id"],
        "probe_report_id": EVIDENCE_KWARGS["probe_report_id"],
        "postbuild_readiness_id": EVIDENCE_KWARGS["postbuild_readiness_id"],
        "index_manifest_id": EVIDENCE_KWARGS["index_manifest_id"],
        "active_table_name": EVIDENCE_KWARGS["active_table_name"],
        "model_name": EVIDENCE_KWARGS["model_name"],
        "model_revision": EVIDENCE_KWARGS["model_revision"],
        "tokenizer_revision": EVIDENCE_KWARGS["tokenizer_revision"],
        "dimension": 1024,
        "dtype": "float32",
        "normalization_mode": "l2",
        "case_pack_id": compute_case_pack_id(cases),
        "case_pack_path": "case_pack.jsonl",
        "phase": "wave2_preparation",
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-02T00:00:00+00:00",
        "db_path": "data/snapshots/catalyst_b2o_7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49.db",
        "lancedb_dir": "data/lancedb_gold/b6g_8ffae891b4e1",
        "embedding_mode": "mock_unit_test",
        "case_count": probe_body.get("case_count", len(cases)),
        "passed_count": probe_body.get("passed_count", len(cases)),
        "nn_result": f"{probe_body.get('passed_count', len(cases))}/{probe_body.get('case_count', len(cases))}",
        "probe_report_path": "probe_report.json",
        "probe_report_sha256": probe_sha,
    }
    if meta_extra:
        meta.update(meta_extra)
    (evidence_dir / "meta.json").write_text(json.dumps(meta, sort_keys=True))
    (evidence_dir / "T4_PROBE_TOKEN.txt").write_text("T4_PROBE_OK\n")
    return evidence_dir


def test_evidence_dir_missing_rejected(tmp_path):
    with pytest.raises(ValueError, match="evidence"):
        validate_t4_evidence(evidence_dir=tmp_path / "missing", current_case_pack=None, resolved=None)


# ---------------------------------------------------------------------------
# AMEND-3: manager-approved T4 contract, probe one-to-one, probe identity,
# complete meta/path contract
# ---------------------------------------------------------------------------


def _probe_body(cases, *, per_case=None,
                corpus_manifest_id=EVIDENCE_KWARGS["corpus_manifest_id"],
                db_sha256=EVIDENCE_KWARGS["db_sha256"],
                case_count=None, passed_count=None, all_passed=True) -> dict:
    per_case = per_case if per_case is not None else [
        CaseProbeResult(c.case_id, c.ticker, c.cutoff, 1).to_dict() for c in cases
    ]
    case_count = case_count if case_count is not None else len(per_case)
    passed_count = passed_count if passed_count is not None else sum(
        1 for entry in per_case if entry.get("ok") is True
    )
    return {
        "schema_version": "served_corpus_probe_v1",
        "corpus_manifest_id": corpus_manifest_id,
        "db_sha256": db_sha256,
        "case_count": case_count,
        "passed_count": passed_count,
        "all_passed": all_passed,
        "per_case": per_case,
    }


def test_probe_duplicate_success_case_10x_rejected(tmp_path):
    """One successful case repeated 10 times must be rejected."""
    from dataclasses import replace

    cases = build_smoke_case_pack(GOLDEN_DIR)
    duplicate = CaseProbeResult(cases[0].case_id, cases[0].ticker, cases[0].cutoff, 1).to_dict()
    evidence_dir = _write_manual_evidence(
        tmp_path / "evidence", cases, _probe_body(cases, per_case=[duplicate] * 10),
    )
    with pytest.raises(ValueError, match="unique|order|case"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=_resolved())


def test_nine_case_evidence_with_ten_probe_rows_rejected(tmp_path):
    """9-case evidence + 10 probe rows must fail."""
    cases = build_smoke_case_pack(GOLDEN_DIR)
    nine = [c for c in cases if c.case_id != "g006"]
    per_case = [CaseProbeResult(c.case_id, c.ticker, c.cutoff, 1).to_dict() for c in cases]
    evidence_dir = _write_manual_evidence(
        tmp_path / "evidence", nine,
        _probe_body(nine, per_case=per_case, case_count=10, passed_count=10),
    )
    with pytest.raises(ValueError, match="10|approved|case"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=nine, resolved=_resolved())


def test_duplicate_case_id_in_case_pack_rejected(tmp_path):
    from dataclasses import replace

    cases = build_smoke_case_pack(GOLDEN_DIR)
    duplicated = [replace(c, case_id="g006") if c.case_id == "g013" else c for c in cases]
    evidence_dir = _write_manual_evidence(
        tmp_path / "evidence", duplicated, _probe_body(duplicated),
    )
    with pytest.raises(ValueError, match="duplicate|case"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=duplicated, resolved=_resolved())


def test_probe_missing_approved_case_rejected(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    per_case = [CaseProbeResult(c.case_id, c.ticker, c.cutoff, 1).to_dict() for c in cases]
    per_case[5] = CaseProbeResult("zzz", "TSLA", cases[5].cutoff, 1).to_dict()
    evidence_dir = _write_manual_evidence(
        tmp_path / "evidence", cases, _probe_body(cases, per_case=per_case),
    )
    with pytest.raises(ValueError, match="case"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=_resolved())


def test_probe_order_set_different_from_case_pack_rejected(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    per_case = [
        CaseProbeResult(c.case_id, c.ticker, c.cutoff, 1).to_dict() for c in reversed(cases)
    ]
    evidence_dir = _write_manual_evidence(
        tmp_path / "evidence", cases, _probe_body(cases, per_case=per_case),
    )
    with pytest.raises(ValueError, match="order|case"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=_resolved())


def test_probe_ok_false_with_positive_count_rejected(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    per_case = [CaseProbeResult(c.case_id, c.ticker, c.cutoff, 1).to_dict() for c in cases]
    per_case[0] = {**per_case[0], "ok": False}
    evidence_dir = _write_manual_evidence(
        tmp_path / "evidence", cases,
        _probe_body(cases, per_case=per_case, passed_count=10),
    )
    with pytest.raises(ValueError, match="ok"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=_resolved())


def test_probe_passed_count_mismatch_actual_ok_rejected(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    evidence_dir = _write_manual_evidence(
        tmp_path / "evidence", cases, _probe_body(cases, passed_count=9),
    )
    with pytest.raises(ValueError, match="passed_count|passed"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=_resolved())


def test_arbitrary_non_approved_10_case_pack_rejected(tmp_path):
    from dataclasses import replace

    cases = build_smoke_case_pack(GOLDEN_DIR)
    renamed = [replace(c, case_id=f"B{i:03d}") for i, c in enumerate(cases, start=1)]
    evidence_dir = _write_manual_evidence(
        tmp_path / "evidence", renamed, _probe_body(renamed),
    )
    with pytest.raises(ValueError, match="approved|case pack|10"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=renamed, resolved=_resolved())


def test_wrong_probe_corpus_manifest_id_recomputed_hash_rejected(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    evidence_dir = _write_manual_evidence(
        tmp_path / "evidence", cases, _probe_body(cases, corpus_manifest_id="e" * 64),
    )
    with pytest.raises(ValueError, match="corpus_manifest_id"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=_resolved())


def test_wrong_probe_db_sha256_recomputed_hash_rejected(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    evidence_dir = _write_manual_evidence(
        tmp_path / "evidence", cases, _probe_body(cases, db_sha256="e" * 64),
    )
    with pytest.raises(ValueError, match="db_sha256|sha"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=_resolved())


@pytest.mark.parametrize("field,bad", [
    ("schema_version", "t4_probe_meta_v0"),
    ("task", "T5"),
    ("phase", "wave1"),
])
def test_meta_schema_task_phase_contract_rejected(tmp_path, field, bad):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    evidence_dir, _, resolved = _build_evidence(tmp_path)
    meta = json.loads((evidence_dir / "meta.json").read_text())
    meta[field] = bad
    (evidence_dir / "meta.json").write_text(json.dumps(meta, sort_keys=True))
    with pytest.raises(ValueError, match=field):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved)


def test_meta_probe_report_path_wrong_rejected(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    evidence_dir, _, resolved = _build_evidence(tmp_path)
    meta = json.loads((evidence_dir / "meta.json").read_text())
    meta["probe_report_path"] = "other.json"
    (evidence_dir / "meta.json").write_text(json.dumps(meta, sort_keys=True))
    with pytest.raises(ValueError, match="probe_report_path"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved)


def test_meta_case_pack_path_absolute_rejected(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    evidence_dir, _, resolved = _build_evidence(tmp_path)
    meta = json.loads((evidence_dir / "meta.json").read_text())
    meta["case_pack_path"] = str(evidence_dir / "case_pack.jsonl")
    (evidence_dir / "meta.json").write_text(json.dumps(meta, sort_keys=True))
    with pytest.raises(ValueError, match="case_pack_path"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved)


def test_meta_db_path_mismatch_resolved_rejected(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    evidence_dir, _, resolved = _build_evidence(tmp_path)
    meta = json.loads((evidence_dir / "meta.json").read_text())
    meta["db_path"] = "data/snapshots/other.db"
    (evidence_dir / "meta.json").write_text(json.dumps(meta, sort_keys=True))
    with pytest.raises(ValueError, match="db_path|path"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved)


def test_meta_lancedb_dir_mismatch_resolved_rejected(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    evidence_dir, _, resolved = _build_evidence(tmp_path)
    meta = json.loads((evidence_dir / "meta.json").read_text())
    meta["lancedb_dir"] = "data/lancedb_gold/other"
    (evidence_dir / "meta.json").write_text(json.dumps(meta, sort_keys=True))
    with pytest.raises(ValueError, match="lancedb_dir|path"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved)


def test_meta_timestamps_parse_and_ordered(tmp_path):
    cases = build_smoke_case_pack(GOLDEN_DIR)
    evidence_dir, _, resolved = _build_evidence(tmp_path)
    meta = json.loads((evidence_dir / "meta.json").read_text())
    meta["completed_at"] = "2020-01-01T00:00:00+00:00"
    (evidence_dir / "meta.json").write_text(json.dumps(meta, sort_keys=True))
    with pytest.raises(ValueError, match="completed_at|started_at"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved)
    meta["completed_at"] = "2026-01-02T00:00:00+00:00"
    meta["started_at"] = "not-a-timestamp"
    (evidence_dir / "meta.json").write_text(json.dumps(meta, sort_keys=True))
    with pytest.raises(ValueError, match="started_at"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=cases, resolved=resolved)


def test_probe_identity_mismatch_rejected_before_model_load_boundary(tmp_path, monkeypatch):
    """A duplicate-probe evidence directory can never enter retrieval/model load."""
    from catalyst_eval.post_import import t4_evidence as t4_mod
    from catalyst_eval.post_import.case_pack import load_case_pack

    cases = build_smoke_case_pack(GOLDEN_DIR)
    duplicate = CaseProbeResult(cases[0].case_id, cases[0].ticker, cases[0].cutoff, 1).to_dict()
    evidence_dir = _write_manual_evidence(
        tmp_path / "evidence", cases, _probe_body(cases, per_case=[duplicate] * 10),
    )
    reloaded = load_case_pack(evidence_dir / "case_pack.jsonl")
    with pytest.raises(ValueError, match="unique|order|case"):
        validate_t4_evidence(evidence_dir=evidence_dir, current_case_pack=reloaded, resolved=_resolved())
