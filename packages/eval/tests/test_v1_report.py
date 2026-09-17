"""M7-8: Stage-1 report write-once publication.

Report JSON uses canonical bytes: an absent target is created atomically,
identical bytes are idempotent, differing bytes raise a conflict without
overwrite. Markdown is a deterministic rendering of JSON and cannot supply
new facts.
"""
from __future__ import annotations

import json
import hashlib
import importlib.util
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from catalyst_eval.v1_1.report import (
    ReportConflictError,
    _sum_known_metrics,
    build_report_payload,
    render_report_markdown,
    scan_report_for_secrets,
    validate_report_gate_evidence,
    write_report_json,
    write_report_markdown,
)


def _payload() -> dict:
    return {
        "schema_version": "v1_1_stage1_report_v1",
        "eval_id": "eval:stage1:v1",
        "hard_gates": {"citation_correctness": True},
        "coverage_limited_count": 1,
        "model_limited_count": 11,
    }


def test_write_report_json_creates_atomically(tmp_path):
    path = tmp_path / "report.json"
    write_report_json(path, _payload())
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["eval_id"] == "eval:stage1:v1"


def test_write_report_json_is_idempotent_for_identical_bytes(tmp_path):
    path = tmp_path / "report.json"
    write_report_json(path, _payload())
    first = path.read_bytes()
    write_report_json(path, _payload())
    assert path.read_bytes() == first


def test_write_report_json_conflicts_without_overwrite(tmp_path):
    path = tmp_path / "report.json"
    write_report_json(path, _payload())
    other = dict(_payload())
    other["eval_id"] = "eval:stage1:v2"
    with pytest.raises(ReportConflictError):
        write_report_json(path, other)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["eval_id"] == "eval:stage1:v1"


def test_write_report_json_loses_race_without_overwriting_winner(
    tmp_path, monkeypatch
):
    """A competing publisher that wins after the pre-check is immutable."""
    from pathlib import Path
    from catalyst_eval.v1_1 import loader as loader_module

    path = tmp_path / "report.json"
    winner = b'{"winner":true}'

    def competing_link(_source, target):
        Path(target).write_bytes(winner)
        raise FileExistsError

    monkeypatch.setattr(loader_module.os, "link", competing_link)
    with pytest.raises(ReportConflictError):
        write_report_json(path, _payload())
    assert path.read_bytes() == winner


def test_markdown_is_deterministic_rendering_of_json(tmp_path):
    payload = _payload()
    md_a = render_report_markdown(payload)
    md_b = render_report_markdown(payload)
    assert md_a == md_b
    # Markdown cannot introduce facts absent from the JSON payload.
    assert "not-a-real-fact" not in md_a
    assert "eval:stage1:v1" in md_a


def test_write_report_markdown_writes_plain_markdown_idempotently(tmp_path):
    path = tmp_path / "report.md"
    markdown = render_report_markdown(_payload())
    write_report_markdown(path, markdown)
    assert path.read_text(encoding="utf-8") == markdown
    assert path.read_bytes().startswith(b"# v1_1_stage1_report_v1")
    write_report_markdown(path, markdown)
    assert path.read_text(encoding="utf-8") == markdown


def test_secret_scan_flags_planted_secret():
    payload = {"text": "Bearer sk-planted-abcdef123456"}
    assert scan_report_for_secrets(payload, secret_values=("sk-planted-abcdef123456",))


def test_secret_scan_accepts_clean_report():
    payload = _payload()
    assert scan_report_for_secrets(payload, secret_values=("sk-real",)) == []


def test_report_metric_aggregation_preserves_unknown_instead_of_zero():
    assert _sum_known_metrics([100, 200]) == 300
    assert _sum_known_metrics([100, None]) is None
    assert _sum_known_metrics([0, 2]) == 2


def test_report_payload_requires_validated_gate_evidence():
    with pytest.raises(ValueError, match="leakage/secret"):
        build_report_payload(
            eval_manifest=None,
            gold_cases=(),
            stratification={},
            ledger=None,
            audits=(),
            execution_head_sha8="head",
        )


def _write_gate_evidence(tmp_path):
    original_path = tmp_path / "leakage_scan.json"
    derived_path = tmp_path / "derived_leakage_scan.json"
    secret_path = tmp_path / "secret_scan.json"
    runtime_db = tmp_path / "runtime.sqlite3"
    original = {
        "n": 2,
        "findings": [
            "trace:c04/diag.terminal.result_status: hidden oracle status",
            "trace:c04/diag.terminal.status_ceiling: hidden oracle status",
        ],
    }
    original_path.write_text(json.dumps(original, sort_keys=True), encoding="utf-8")
    source_payload = {
        "schema_version": "v1.1_run_diagnostics_v1",
        "terminal": {"terminal_event_type": "run.completed"},
    }
    source_json = json.dumps(source_payload, sort_keys=True, separators=(",", ":"))
    source_hash = hashlib.sha256(source_json.encode()).hexdigest()
    derived = {
        "schema_version": "m7_derived_leakage_scan_v2",
        "derived": True,
        "original_scan_path": str(original_path),
        "original_scan_sha256": hashlib.sha256(original_path.read_bytes()).hexdigest(),
        "original_scan_n": 2,
        "n": 0,
        "findings": [],
        "c04_rendered_messages_findings": [],
        "hidden_gold_boundary": [],
        "model_visible_prompt": False,
        "classifier": "verified_post_writer_run_diagnostics_terminal_status_exemption_v2",
        "exemption_contract": {
            "artifact_type": "run_diagnostics",
            "post_writer_persisted": True,
            "schema_version": "v1.1_run_diagnostics_v1",
            "terminal_event_type": "run.completed",
            "path_only_exemption": False,
            "generic_or_model_visible_trace_same_fields": "reported",
            "exempted_paths": ["terminal.result_status", "terminal.status_ceiling"],
        },
        "source_artifact": {
            "artifact_id": "diagnostics:run-1",
            "run_id": "run-1",
            "event_seq": 3,
            "artifact_type": "run_diagnostics",
            "payload_hash": source_hash,
            "schema_version": "v1.1_run_diagnostics_v1",
            "terminal_event_type": "run.completed",
            "post_writer_persisted": True,
        },
    }
    derived_path.write_text(json.dumps(derived, sort_keys=True), encoding="utf-8")
    secret_path.write_text(json.dumps({"findings": []}), encoding="utf-8")
    conn = sqlite3.connect(runtime_db)
    conn.executescript(
        """
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, lifecycle_status TEXT NOT NULL);
        CREATE TABLE run_events (
            run_id TEXT NOT NULL, seq INTEGER NOT NULL, event_type TEXT NOT NULL,
            PRIMARY KEY (run_id, seq)
        );
        CREATE TABLE run_artifacts (
            artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, event_seq INTEGER NOT NULL,
            artifact_type TEXT NOT NULL, payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO runs VALUES (?, ?)", ("run-1", "COMPLETED"))
    conn.execute("INSERT INTO run_events VALUES (?, ?, ?)", ("run-1", 3, "run.completed"))
    conn.execute(
        "INSERT INTO run_artifacts VALUES (?, ?, ?, ?, ?, ?)",
        ("diagnostics:run-1", "run-1", 3, "run_diagnostics", source_hash, source_json),
    )
    conn.commit()
    conn.close()
    return original_path, derived_path, secret_path, runtime_db


def _write_handoff_manifest(tmp_path, original, secret, runtime_db, *, head="head", eval_id="eval"):
    entries = []
    for relative, path in (
        ("report_inputs/leakage_scan.json", original),
        ("report_inputs/secret_scan.json", secret),
        ("runtime.sqlite3", runtime_db),
    ):
        entries.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    manifest = {
        "schema_version": "m7_stage1_final_handoff_manifest_v1",
        "head": head,
        "eval_id": eval_id,
        "file_count": len(entries),
        "files": entries,
        "missing_required": [],
        "secret_scan_hits": [],
    }
    path = tmp_path / "final_handoff_manifest.json"
    path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return path


def _handoff_args(tmp_path, *, head="head", eval_id="eval"):
    original, derived, secret, runtime_db = _write_gate_evidence(tmp_path)
    handoff = _write_handoff_manifest(
        tmp_path, original, secret, runtime_db, head=head, eval_id=eval_id
    )
    return original, derived, secret, runtime_db, handoff


def _validate_with_handoff(original, derived, secret, runtime_db, handoff, *, expected_sha=None, expected_head="head", expected_eval_id="eval"):
    return validate_report_gate_evidence(
        leakage_scan_path=original,
        derived_leakage_scan_path=derived,
        secret_scan_path=secret,
        runtime_db_path=runtime_db,
        handoff_manifest_path=handoff,
        handoff_manifest_sha256=expected_sha
        or hashlib.sha256(handoff.read_bytes()).hexdigest(),
        expected_execution_head=expected_head,
        expected_eval_id=expected_eval_id,
    )


def test_report_gate_evidence_accepts_valid_handoff_manifest(tmp_path):
    original, derived, secret, runtime_db, handoff = _handoff_args(tmp_path)
    evidence = _validate_with_handoff(
        original, derived, secret, runtime_db, handoff
    )
    assert evidence["handoff_manifest"]["file_hashes"] == {
        "report_inputs/leakage_scan.json": hashlib.sha256(original.read_bytes()).hexdigest(),
        "report_inputs/secret_scan.json": hashlib.sha256(secret.read_bytes()).hexdigest(),
        "runtime.sqlite3": hashlib.sha256(runtime_db.read_bytes()).hexdigest(),
    }


def test_report_gate_evidence_rejects_wrong_expected_handoff_sha(tmp_path):
    original, derived, secret, runtime_db, handoff = _handoff_args(tmp_path)
    with pytest.raises(ValueError, match="handoff manifest SHA"):
        _validate_with_handoff(
            original, derived, secret, runtime_db, handoff, expected_sha="0" * 64
        )


@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        ("head", "head"),
        ("eval_id", "eval_id"),
        ("duplicate", "duplicate"),
    ),
)
def test_report_gate_evidence_rejects_invalid_handoff_identity_and_inventory(tmp_path, mutation, match):
    original, derived, secret, runtime_db, handoff = _handoff_args(tmp_path)
    payload = json.loads(handoff.read_text(encoding="utf-8"))
    if mutation == "head":
        payload["head"] = "wrong-head"
    elif mutation == "eval_id":
        payload["eval_id"] = "wrong-eval"
    else:
        payload["files"].append(dict(payload["files"][0]))
        payload["file_count"] += 1
    handoff.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        _validate_with_handoff(original, derived, secret, runtime_db, handoff)


@pytest.mark.parametrize("mutation", ("secret", "runtime", "leakage"))
def test_report_gate_evidence_rejects_substituted_or_modified_handoff_files(tmp_path, mutation):
    original, derived, secret, runtime_db, handoff = _handoff_args(tmp_path)
    if mutation == "secret":
        secret.write_bytes(b'{"findings":[]}\n')
    elif mutation == "runtime":
        with runtime_db.open("ab") as handle:
            handle.write(b"substituted")
    else:
        original.write_text(json.dumps({"n": 0, "findings": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="handoff file"):
        _validate_with_handoff(original, derived, secret, runtime_db, handoff)


def test_report_gate_evidence_accepts_real_handoff_manifest():
    repo = Path(__file__).resolve().parents[3]
    output = repo / "data" / "eval_reports" / "v1_1_stage1_327b3eed"
    evidence = validate_report_gate_evidence(
        leakage_scan_path=output / "report_inputs" / "leakage_scan.json",
        derived_leakage_scan_path=output / "derived" / "leakage_scan_after_terminal_status_exemption.json",
        secret_scan_path=output / "report_inputs" / "secret_scan.json",
        runtime_db_path=output / "runtime.sqlite3",
        handoff_manifest_path=output / "final_handoff_manifest.json",
        handoff_manifest_sha256="b745902af95e158d33ea68226b4991d933a3e3ed30887429571a3da39f5d6baf",
        expected_execution_head="327b3eed6929b53b1c1b5ea7fd2c57fa1c7bec8e",
        expected_eval_id="2792d333e7c01eb528fce77b5a54531343e6acdc2633212d2d3315fc97bb32cc",
    )
    assert evidence["handoff_manifest"]["sha256"] == "b745902af95e158d33ea68226b4991d933a3e3ed30887429571a3da39f5d6baf"


def test_report_gate_evidence_is_identity_bound_and_clean(tmp_path):
    original, derived, secret, runtime_db, handoff = _handoff_args(tmp_path)
    evidence = validate_report_gate_evidence(
        leakage_scan_path=original,
        derived_leakage_scan_path=derived,
        secret_scan_path=secret,
        runtime_db_path=runtime_db,
        handoff_manifest_path=handoff,
        handoff_manifest_sha256=hashlib.sha256(handoff.read_bytes()).hexdigest(),
        expected_execution_head="head",
        expected_eval_id="eval",
    )
    assert evidence["leakage_scan"]["status"] == "PASS"
    assert evidence["leakage_scan"]["original_count"] == 2
    assert evidence["leakage_scan"]["model_visible_count"] == 0
    assert evidence["secret_scan"] == {
        "status": "PASS",
        "count": 0,
        "sha256": hashlib.sha256(secret.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize(
    "mutation",
    (
        "original_hash",
        "artifact_hash",
        "schema",
        "provenance",
        "rendered_findings",
        "hidden_gold",
        "secret_findings",
    ),
)
def test_report_gate_evidence_rejects_unverified_or_leaking_inputs(tmp_path, mutation):
    original, derived, secret, runtime_db, handoff = _handoff_args(tmp_path)
    payload = json.loads(derived.read_text(encoding="utf-8"))
    if mutation == "original_hash":
        payload["original_scan_sha256"] = "0" * 64
    elif mutation == "artifact_hash":
        payload["source_artifact"]["payload_hash"] = "0" * 64
    elif mutation == "schema":
        payload["exemption_contract"]["schema_version"] = "wrong"
    elif mutation == "provenance":
        payload["exemption_contract"]["post_writer_persisted"] = False
    elif mutation == "rendered_findings":
        payload["c04_rendered_messages_findings"] = ["leak"]
    elif mutation == "hidden_gold":
        payload["hidden_gold_boundary"] = ["leak"]
    elif mutation == "secret_findings":
        secret.write_text(json.dumps({"findings": ["secret"]}), encoding="utf-8")
    derived.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError):
        validate_report_gate_evidence(
            leakage_scan_path=original,
            derived_leakage_scan_path=derived,
            secret_scan_path=secret,
            runtime_db_path=runtime_db,
            handoff_manifest_path=handoff,
            handoff_manifest_sha256=hashlib.sha256(handoff.read_bytes()).hexdigest(),
            expected_execution_head="head",
            expected_eval_id="eval",
        )


def _load_stage1_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_v1_1_stage1.py"
    spec = importlib.util.spec_from_file_location("stage1_report_cli", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_real_c01_c12_report_appends_observed_manifest_outcome(tmp_path):
    """The authoritative 12-case report path appends observed bindings once."""
    repo = Path(__file__).resolve().parents[3]
    output = repo / "data" / "eval_reports" / "v1_1_stage1_327b3eed"
    module = _load_stage1_cli()
    audit = repo / "data" / "baseline" / "reports" / (
        "v1_1_stage1_327b3eed_output_audit.jsonl"
    )
    manifest_out = tmp_path / "eval_manifest.json"
    report_json = tmp_path / "report.json"
    report_md = tmp_path / "report.md"
    argv = [
        "report",
        "--output-dir", str(output),
        "--audit", str(audit),
        "--manifest-out", str(manifest_out),
        "--json-out", str(report_json),
        "--markdown-out", str(report_md),
        "--leakage-scan", str(output / "report_inputs" / "leakage_scan.json"),
        "--derived-leakage-scan", str(
            output / "derived" / "leakage_scan_after_terminal_status_exemption.json"
        ),
        "--secret-scan", str(output / "report_inputs" / "secret_scan.json"),
        "--runtime-evidence-db", str(output / "runtime.sqlite3"),
        "--handoff-manifest", str(output / "final_handoff_manifest.json"),
        "--handoff-manifest-sha256",
        "b745902af95e158d33ea68226b4991d933a3e3ed30887429571a3da39f5d6baf",
    ]
    assert module.main(argv) == 1
    manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
    report = json.loads(report_json.read_text(encoding="utf-8"))
    outcome = manifest["outcome"]
    assert outcome is not None
    assert all(
        not refs for refs in manifest["run_artifact_identity"].values()
    )
    expected_case_ids = [f"c{i:02d}" for i in range(1, 13)]
    refs = outcome["per_case_result_refs"]
    bindings = outcome["observed_run_artifact_identity"]["run_manifest_bindings"]
    assert [ref["case_id"] for ref in refs] == expected_case_ids
    assert len(bindings) == len(refs) == 12
    assert [ref["run_manifest_id"] for ref in refs] == [
        binding["run_manifest_id"] for binding in bindings
    ]
    assert [ref["run_manifest_hash"] for ref in refs] == [
        binding["run_manifest_hash"] for binding in bindings
    ]
    for ref, binding in zip(refs, bindings):
        assert ref["result_artifact_id"]
        assert ref["run_manifest_id"] == binding["run_manifest_id"]
    observed_identity = outcome["observed_run_artifact_identity"]
    assert len(observed_identity["context_pack_refs"]) == 12
    assert len(observed_identity["claim_plan_refs"]) == 12
    assert len(observed_identity["assurance_refs"]) == 12
    assert outcome["latency_tokens_cost"] == {
        "total_latency_ms": report["latency_ms"],
        "total_tokens": report["tokens"],
        "total_cost": report["cost_usd"],
    }
    aggregate_by_id = {
        metric["metric_id"]: metric for metric in outcome["aggregate_metrics"]
    }
    canonical_metrics = {}
    for section in ("attribution_metrics", "trajectory_metrics", "retrieval_metrics"):
        canonical_metrics.update(report[section])
    for metric in canonical_metrics.values():
        if metric["denominator"] <= 0:
            assert metric["metric_id"] not in aggregate_by_id
            continue
        observed = aggregate_by_id[metric["metric_id"]]
        for field in (
            "numerator", "denominator", "eligible_count", "excluded_count",
            "non_scorable_count",
        ):
            assert observed[field] == metric[field]
        assert observed["hard_gate_passed"] == metric.get("gate_passed")
    assert {
        gate["gate_id"]: gate["passed"] for gate in outcome["gate_results"]
    } == report["hard_gates"]
    assert outcome["completed_at"] == "2026-09-15T18:03:09.523892Z"
    first_outputs = (manifest_out.read_bytes(), report_json.read_bytes(), report_md.read_bytes())
    assert module.main(argv) == 1
    assert (manifest_out.read_bytes(), report_json.read_bytes(), report_md.read_bytes()) == first_outputs


def test_real_report_outcome_rejects_invalid_persisted_bindings():
    """The observed outcome builder rejects all malformed ledger binding shapes."""
    repo = Path(__file__).resolve().parents[3]
    output = repo / "data" / "eval_reports" / "v1_1_stage1_327b3eed"
    module = _load_stage1_cli()
    summary = module._load_prepare_summary(output)
    eval_manifest, _ = module._load_eval_manifest_and_cases(summary, output)
    ledger = module.ExecutionLedger.load(
        output / "execution_ledger.jsonl",
        expected_eval_id=eval_manifest.evaluation_identity.eval_id,
    )
    report = json.loads(
        (repo / "data" / "baseline" / "reports" / "v1_1_stage1_327b3eed.json").read_text(
            encoding="utf-8"
        )
    )
    runtime_db = output / "runtime.sqlite3"
    rows = list(ledger.rows)
    with pytest.raises(ValueError, match="ordered|case"):
        module._build_observed_eval_outcome(
            eval_manifest, module.ExecutionLedger(rows=tuple(reversed(rows))), report, runtime_db
        )
    with pytest.raises(ValueError, match="duplicate|case"):
        module._build_observed_eval_outcome(
            eval_manifest, module.ExecutionLedger(rows=(rows[0], *rows)), report, runtime_db
        )
    with pytest.raises(ValueError, match="identity|manifest|binding|checksum"):
        module._build_observed_eval_outcome(
            eval_manifest,
            module.ExecutionLedger(rows=(replace(rows[0], run_manifest_id="manifest:wrong"), *rows[1:])),
            report,
            runtime_db,
        )
    with pytest.raises(ValueError, match="artifact ref"):
        module._build_observed_eval_outcome(
            eval_manifest,
            module.ExecutionLedger(rows=(replace(rows[0], context_pack_ref=None), *rows[1:])),
            report,
            runtime_db,
        )
