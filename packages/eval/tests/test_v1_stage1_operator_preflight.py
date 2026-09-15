"""M7 Phase A: Stage-1 operator preflight correctness (RED-first).

Covers the Phase A corrective contract:

- ``--runtime-db`` is mandatory for ``prepare`` and ``all``;
- ``prepare`` streams the runtime DB hash rather than reading it whole;
- the real Q-001 runtime identity must be declared (manifest or explicit
  operator flags) and its hash must bind to the runtime DB content; the
  ``runtime-id:stage1`` / ``dddd...`` fallback identities are gone;
- new V1.1 surfaces resolve benchmark-named files (``cases.jsonl`` /
  ``stratification.json``) while sealed legacy names stay readable;
- stratification can be derived from the manifest or its sibling file;
- the production M6 app/SSE operator factory is directly wired and
  fail-closed when required configuration is absent.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

from tests.v1_1_fixtures import (
    make_dataset_manifest,
    make_stage1_cases,
    make_stratification,
)


def _load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_v1_1_stage1.py"
    spec = importlib.util.spec_from_file_location(
        "run_v1_1_stage1_operator", path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _db_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_runtime_db(tmp_path: Path, name: str = "runtime.db") -> tuple[Path, str]:
    db = tmp_path / name
    db.write_bytes(b"catalyst-runtime-db\x00" * 64)
    return db, _db_sha256(db)


def _write_pricing(tmp_path: Path) -> Path:
    """Non-secret pricing input for the shared provider budget guard."""
    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps(
            {
                "models": {
                    "deepseek/deepseek-chat": {
                        "input_usd_per_million_tokens": 1.0,
                        "output_usd_per_million_tokens": 2.0,
                    }
                },
                "role_token_limits": {
                    "evidence_analyst": {
                        "max_input_tokens": 1000,
                        "max_output_tokens": 500,
                    },
                    "streaming_writer": {
                        "max_input_tokens": 1000,
                        "max_output_tokens": 500,
                    },
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _manifest_with_identity(
    rows, stratification, *, ref: str, digest: str
) -> dict:
    manifest = make_dataset_manifest(rows, stratification)
    manifest["data_runtime_identity_ref"] = ref
    manifest["data_runtime_identity_hash"] = digest
    manifest["q001_file_sha256"] = digest
    return manifest


def _write_legacy_dataset(tmp_path: Path, rows, manifest) -> tuple[Path, Path]:
    cases_path = tmp_path / "v1_1_stage1_cases.jsonl"
    cases_path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "v1_1_stage1_dataset_manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    strat_path = tmp_path / "v1_1_stage1_stratification.json"
    if not strat_path.exists():
        strat_path.write_text(
            json.dumps(make_stratification(rows), sort_keys=True), encoding="utf-8"
        )
    return cases_path, manifest_path


def _write_benchmark_dataset(tmp_path: Path, rows, manifest) -> Path:
    bench = tmp_path / "benchmarks" / "v1_1" / "stage1"
    bench.mkdir(parents=True)
    (bench / "cases.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
    )
    manifest["schema_version"] = "v1_1_benchmark_dataset_manifest_v1"
    (bench / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    return bench / "manifest.json"


# ---------------------------------------------------------------------------
# mandatory --runtime-db
# ---------------------------------------------------------------------------

def test_prepare_requires_runtime_db(tmp_path):
    module = _load_cli()
    rows = make_stage1_cases()
    strat = make_stratification(rows)
    manifest = _manifest_with_identity(
        rows, strat, ref="v1:corpus:q011", digest="a" * 64
    )
    _cases, manifest_path = _write_legacy_dataset(tmp_path, rows, manifest)
    exit_code = module.main([
        "prepare",
        "--dataset-manifest", str(manifest_path),
        "--stratification", str(tmp_path / "v1_1_stage1_stratification.json"),
        "--output-dir", str(tmp_path / "out"),
        "--max-provider-calls", "100",
        "--max-cost-usd", "10.0",
    ])
    assert exit_code == 2
    assert not (tmp_path / "out" / "prepare_summary.json").exists()


def test_all_requires_runtime_db(tmp_path):
    module = _load_cli()
    rows = make_stage1_cases()
    strat = make_stratification(rows)
    manifest = _manifest_with_identity(
        rows, strat, ref="v1:corpus:q011", digest="a" * 64
    )
    _cases, manifest_path = _write_legacy_dataset(tmp_path, rows, manifest)
    exit_code = module.main([
        "all",
        "--dataset-manifest", str(manifest_path),
        "--stratification", str(tmp_path / "v1_1_stage1_stratification.json"),
        "--output-dir", str(tmp_path / "out"),
        "--max-provider-calls", "100",
        "--max-cost-usd", "10.0",
    ])
    assert exit_code == 2


def test_prepare_missing_runtime_db_file_is_operator_error(tmp_path):
    module = _load_cli()
    rows = make_stage1_cases()
    strat = make_stratification(rows)
    manifest = _manifest_with_identity(
        rows, strat, ref="v1:corpus:q011", digest="a" * 64
    )
    _cases, manifest_path = _write_legacy_dataset(tmp_path, rows, manifest)
    exit_code = module.main([
        "prepare",
        "--dataset-manifest", str(manifest_path),
        "--stratification", str(tmp_path / "v1_1_stage1_stratification.json"),
        "--output-dir", str(tmp_path / "out"),
        "--max-provider-calls", "100",
        "--max-cost-usd", "10.0",
        "--runtime-db", str(tmp_path / "nope.db"),
    ])
    assert exit_code == 2


# ---------------------------------------------------------------------------
# incremental (non read_bytes) runtime DB hashing
# ---------------------------------------------------------------------------

def test_prepare_hashes_runtime_db_incrementally(tmp_path, monkeypatch):
    module = _load_cli()
    rows = make_stage1_cases()
    strat = make_stratification(rows)
    db, digest = _make_runtime_db(tmp_path)
    manifest = _manifest_with_identity(
        rows, strat, ref="v1:corpus:q011", digest=digest
    )
    _cases, manifest_path = _write_legacy_dataset(tmp_path, rows, manifest)

    db_path = db.resolve()
    original_open = io.open
    read_counts: dict[str, int] = {}

    class _CountingReader:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *exc):
            return self._handle.__exit__(*exc)

        def read(self, *args, **kwargs):
            read_counts[str(db_path)] = read_counts.get(str(db_path), 0) + 1
            return self._handle.read(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._handle, name)

    def counting_open(file, *args, **kwargs):
        handle = original_open(file, *args, **kwargs)
        try:
            resolved = Path(file).resolve()
        except (TypeError, ValueError):
            return handle
        if resolved == db_path:
            return _CountingReader(handle)
        return handle

    monkeypatch.setattr(io, "open", counting_open)
    exit_code = module.main([
        "prepare",
        "--dataset-manifest", str(manifest_path),
        "--stratification", str(tmp_path / "v1_1_stage1_stratification.json"),
        "--output-dir", str(tmp_path / "out"),
        "--max-provider-calls", "100",
        "--max-cost-usd", "10.0",
        "--runtime-db", str(db),
    ])
    assert exit_code == 0
    assert read_counts.get(str(db_path), 0) >= 2, (
        "runtime DB must be hashed incrementally in chunks, not read whole"
    )
    summary = json.loads(
        (tmp_path / "out" / "prepare_summary.json").read_text(encoding="utf-8")
    )
    assert summary["data_runtime_identity_object_hash"] == digest
    assert summary["q001_file_sha256"] == digest
    assert "provider_configured" not in summary


# ---------------------------------------------------------------------------
# real runtime identity (no fallback) + mismatch fails closed
# ---------------------------------------------------------------------------

def test_prepare_rejects_missing_declared_runtime_identity(tmp_path):
    module = _load_cli()
    rows = make_stage1_cases()
    strat = make_stratification(rows)
    manifest = make_dataset_manifest(rows, strat)  # no identity fields
    _cases, manifest_path = _write_legacy_dataset(tmp_path, rows, manifest)
    _db, _digest = _make_runtime_db(tmp_path)
    exit_code = module.main([
        "prepare",
        "--dataset-manifest", str(manifest_path),
        "--stratification", str(tmp_path / "v1_1_stage1_stratification.json"),
        "--output-dir", str(tmp_path / "out"),
        "--max-provider-calls", "100",
        "--max-cost-usd", "10.0",
        "--runtime-db", str(tmp_path / "runtime.db"),
    ])
    assert exit_code == 2
    assert not (tmp_path / "out" / "prepare_summary.json").exists()


def test_prepare_runtime_db_hash_mismatch_fails_closed(tmp_path):
    module = _load_cli()
    rows = make_stage1_cases()
    strat = make_stratification(rows)
    db, _digest = _make_runtime_db(tmp_path)
    manifest = _manifest_with_identity(
        rows, strat, ref="v1:corpus:q011", digest="b" * 64
    )
    _cases, manifest_path = _write_legacy_dataset(tmp_path, rows, manifest)
    exit_code = module.main([
        "prepare",
        "--dataset-manifest", str(manifest_path),
        "--stratification", str(tmp_path / "v1_1_stage1_stratification.json"),
        "--output-dir", str(tmp_path / "out"),
        "--max-provider-calls", "100",
        "--max-cost-usd", "10.0",
        "--runtime-db", str(db),
    ])
    assert exit_code == 2


def test_prepare_never_uses_legacy_fallback_identity(tmp_path):
    """The ``runtime-id:stage1`` ref and ``d``*64 hash fallbacks are removed:
    a manifest that omits the declared identity fails closed even when a
    runtime DB is supplied."""
    module = _load_cli()
    rows = make_stage1_cases()
    strat = make_stratification(rows)
    db, _digest = _make_runtime_db(tmp_path)
    manifest = make_dataset_manifest(rows, strat)  # no identity fields
    assert "data_runtime_identity_ref" not in manifest
    _cases, manifest_path = _write_legacy_dataset(tmp_path, rows, manifest)
    exit_code = module.main([
        "prepare",
        "--dataset-manifest", str(manifest_path),
        "--stratification", str(tmp_path / "v1_1_stage1_stratification.json"),
        "--output-dir", str(tmp_path / "out"),
        "--max-provider-calls", "100",
        "--max-cost-usd", "10.0",
        "--runtime-db", str(db),
    ])
    assert exit_code == 2
    assert not (tmp_path / "out" / "prepare_summary.json").exists()


def test_prepare_declared_identity_is_recorded_without_fallback(tmp_path):
    module = _load_cli()
    rows = make_stage1_cases()
    strat = make_stratification(rows)
    db, digest = _make_runtime_db(tmp_path)
    manifest = _manifest_with_identity(
        rows, strat, ref="v1:corpus:q011", digest=digest
    )
    _cases, manifest_path = _write_legacy_dataset(tmp_path, rows, manifest)
    exit_code = module.main([
        "prepare",
        "--dataset-manifest", str(manifest_path),
        "--stratification", str(tmp_path / "v1_1_stage1_stratification.json"),
        "--output-dir", str(tmp_path / "out"),
        "--max-provider-calls", "100",
        "--max-cost-usd", "10.0",
        "--runtime-db", str(db),
    ])
    assert exit_code == 0
    summary = json.loads(
        (tmp_path / "out" / "prepare_summary.json").read_text(encoding="utf-8")
    )
    assert summary["data_runtime_identity_ref"] == "v1:corpus:q011"
    assert summary["data_runtime_identity_object_hash"] == digest
    assert summary["data_runtime_identity_ref"] != "runtime-id:stage1"
    assert summary["data_runtime_identity_object_hash"] != "d" * 64


def test_q001_file_sha_and_data_runtime_object_hash_are_distinct_bindings(tmp_path):
    module = _load_cli()
    db, file_sha = _make_runtime_db(tmp_path, name="q001-derivative.db")
    object_sha = "a" * 64
    ref, resolved_object_sha = module._resolve_runtime_identity(
        {"data_runtime_identity_ref": "v1:object", "data_runtime_identity_hash": object_sha},
        declaration_ref=None,
        declaration_hash=None,
    )
    assert (ref, resolved_object_sha) == ("v1:object", object_sha)
    assert module._verify_q001_file_identity(
        runtime_db=db, declared_file_sha256=file_sha
    )[1] == file_sha
    with pytest.raises(module.Stage1OperatorError, match="Q-001.*file|SHA"):
        module._verify_q001_file_identity(
            runtime_db=db, declared_file_sha256=object_sha
        )


# ---------------------------------------------------------------------------
# benchmark-named resolution + legacy compatibility + stratification derivation
# ---------------------------------------------------------------------------

def test_prepare_resolves_benchmark_named_dataset(tmp_path):
    module = _load_cli()
    rows = make_stage1_cases()
    strat = make_stratification(rows)
    db, digest = _make_runtime_db(tmp_path)
    manifest = _manifest_with_identity(
        rows, strat, ref="v1:corpus:q011", digest=digest
    )
    manifest_path = _write_benchmark_dataset(tmp_path, rows, manifest)
    exit_code = module.main([
        "prepare",
        "--dataset-manifest", str(manifest_path),
        "--output-dir", str(tmp_path / "out"),
        "--max-provider-calls", "100",
        "--max-cost-usd", "10.0",
        "--runtime-db", str(db),
    ])
    assert exit_code == 0
    summary = json.loads(
        (tmp_path / "out" / "prepare_summary.json").read_text(encoding="utf-8")
    )
    assert summary["case_count"] == 12


def test_prepare_legacy_named_dataset_still_resolves(tmp_path):
    module = _load_cli()
    rows = make_stage1_cases()
    strat = make_stratification(rows)
    db, digest = _make_runtime_db(tmp_path)
    manifest = _manifest_with_identity(
        rows, strat, ref="v1:corpus:q011", digest=digest
    )
    _cases, manifest_path = _write_legacy_dataset(tmp_path, rows, manifest)
    exit_code = module.main([
        "prepare",
        "--dataset-manifest", str(manifest_path),
        "--output-dir", str(tmp_path / "out"),
        "--max-provider-calls", "100",
        "--max-cost-usd", "10.0",
        "--runtime-db", str(db),
    ])
    assert exit_code == 0


def test_prepare_derives_stratification_from_manifest(tmp_path):
    module = _load_cli()
    rows = make_stage1_cases()
    strat = make_stratification(rows)
    db, digest = _make_runtime_db(tmp_path)
    manifest = _manifest_with_identity(
        rows, strat, ref="v1:corpus:q011", digest=digest
    )
    cases_path = tmp_path / "v1_1_stage1_cases.jsonl"
    cases_path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "v1_1_stage1_dataset_manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    # No --stratification flag: the manifest carries the stratification block.
    exit_code = module.main([
        "prepare",
        "--dataset-manifest", str(manifest_path),
        "--output-dir", str(tmp_path / "out"),
        "--max-provider-calls", "100",
        "--max-cost-usd", "10.0",
        "--runtime-db", str(db),
    ])
    assert exit_code == 0


# ---------------------------------------------------------------------------
# production operator factory wiring
# ---------------------------------------------------------------------------

def test_execute_default_factory_receives_runtime_db_path(tmp_path, monkeypatch):
    """execute must build the live adapter through the operator factory,
    passing the prepare-recorded runtime DB path (not an invented one)."""
    module = _load_cli()
    rows = make_stage1_cases()
    strat = make_stratification(rows)
    db, digest = _make_runtime_db(tmp_path)
    object_hash = "a" * 64
    manifest = _manifest_with_identity(
        rows, strat, ref="v1:corpus:q011", digest=object_hash
    )
    manifest["q001_file_sha256"] = digest
    _cases, manifest_path = _write_legacy_dataset(tmp_path, rows, manifest)
    strat_path = tmp_path / "v1_1_stage1_stratification.json"
    out_dir = tmp_path / "out"
    assert module.main([
        "prepare",
        "--dataset-manifest", str(manifest_path),
        "--stratification", str(strat_path),
        "--output-dir", str(out_dir),
        "--max-provider-calls", "1",
        "--max-cost-usd", "1.0",
        "--runtime-db", str(db),
    ]) == 0

    calls: list[dict] = []

    def spy_factory(**kwargs):
        calls.append(kwargs)
        raise module.Stage1OperatorError("stop after wiring check")

    monkeypatch.setattr(module, "_default_runner_adapter_factory", spy_factory)
    exit_code = module.main([
        "execute",
        "--output-dir", str(out_dir),
        "--max-provider-calls", "1",
        "--max-cost-usd", "1.0",
        "--pricing-json", str(_write_pricing(tmp_path)),
    ])
    assert exit_code == 2
    assert calls, "execute must call the operator factory with the runtime DB paths"
    # The immutable Q-001 derivative and the writable M6 runtime DB are
    # separate files; the adapter's db_path is the writable runtime DB.
    assert str(calls[0]["data_db_path"]) == str(db.resolve())
    assert str(calls[0]["runtime_db_path"]) == str(
        (out_dir / "runtime.sqlite3").resolve()
    )
    assert calls[0]["db_path"] == calls[0]["runtime_db_path"]
    assert calls[0]["prepared_identity_ref"] == "v1:corpus:q011"
    assert calls[0]["prepared_identity_hash"] == object_hash
    assert calls[0]["prepared_identity_hash"] != digest


def test_production_operator_factory_fails_closed_without_config(
    tmp_path, monkeypatch
):
    module = _load_cli()
    for name in (
        "CATALYST_LANCEDB_DIR",
        "CATALYST_CORPUS_MANIFEST_ID",
        "CATALYST_INDEX_MANIFEST_ID",
        "CATALYST_SOURCE_BUNDLE_ID",
        "CATALYST_SNAPSHOT_ID",
        "CATALYST_PROBE_REPORT_ID",
        "CATALYST_POSTBUILD_READINESS_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    data_db, _digest = _make_runtime_db(tmp_path, name="q001-derivative.db")
    with pytest.raises(module.Stage1OperatorError):
        module.build_operator_runner_adapter(
            data_db_path=data_db,
            runtime_db_path=tmp_path / "runtime.sqlite3",
        )


def test_production_operator_factory_wires_index_manifest_path(
    tmp_path, monkeypatch
):
    """The live operator must use the authoritative clean-import manifest."""
    module = _load_cli()
    for name in module._OPERATOR_REQUIRED_ENV:
        monkeypatch.setenv(name, "approved")
    manifest_path = tmp_path / "index_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CATALYST_INDEX_MANIFEST_PATH", str(manifest_path))
    captured = {}

    class SpyLoader:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import catalyst_agents.runtime.dependencies as dependencies

    monkeypatch.setattr(dependencies, "RuntimeDependencyLoader", SpyLoader)
    data_db, _digest = _make_runtime_db(tmp_path, name="q001-derivative.db")
    adapter = module.build_operator_runner_adapter(
        data_db_path=data_db,
        runtime_db_path=tmp_path / "runtime.sqlite3",
        composition_builder=lambda **kwargs: object(),
        adapter_factory=lambda **kwargs: kwargs,
    )
    assert captured["index_manifest_path"] == manifest_path
    from catalyst_agents.runtime.query_embedding import (
        ProductionBgeM3QueryEmbeddingFactory,
    )

    assert isinstance(
        captured["query_embedding_factory"],
        ProductionBgeM3QueryEmbeddingFactory,
    )
    assert adapter["provider"] == "deepseek"
    assert adapter["model_id"] == "deepseek-flash"


def test_production_operator_factory_rejects_shared_data_and_runtime_db(
    tmp_path, monkeypatch
):
    """The immutable data DB can never be the writable runtime DB."""
    module = _load_cli()
    monkeypatch.setenv("CATALYST_LANCEDB_DIR", "/tmp/lancedb")
    for name in (
        "CATALYST_CORPUS_MANIFEST_ID",
        "CATALYST_INDEX_MANIFEST_ID",
        "CATALYST_SOURCE_BUNDLE_ID",
        "CATALYST_SNAPSHOT_ID",
        "CATALYST_PROBE_REPORT_ID",
        "CATALYST_POSTBUILD_READINESS_ID",
    ):
        monkeypatch.setenv(name, "approved")
    data_db, _digest = _make_runtime_db(tmp_path, name="q001-derivative.db")
    with pytest.raises(module.Stage1OperatorError, match="separate"):
        module.build_operator_runner_adapter(
            data_db_path=data_db, runtime_db_path=data_db
        )


def test_production_credential_resolver_reads_stripped_env_value_only(
    tmp_path, monkeypatch
):
    module = _load_cli()
    for name in module._OPERATOR_REQUIRED_ENV:
        monkeypatch.setenv(name, "approved")
    monkeypatch.setenv("CATALYST_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "  sentinel-value  ")
    captured = {}

    def make_adapter(**kwargs):
        captured.update(kwargs)
        return kwargs

    db, _digest = _make_runtime_db(tmp_path, name="q001-derivative.db")
    adapter = module.build_operator_runner_adapter(
        data_db_path=db,
        runtime_db_path=tmp_path / "runtime.sqlite3",
        composition_builder=lambda **kwargs: object(),
        dependency_loader_builder=lambda **kwargs: object(),
        adapter_factory=make_adapter,
    )
    assert adapter == captured
    assert captured["credential_provider"]() == "sentinel-value"


def test_production_credential_resolver_fails_closed_when_value_is_missing(
    tmp_path, monkeypatch
):
    module = _load_cli()
    for name in module._OPERATOR_REQUIRED_ENV:
        monkeypatch.setenv(name, "approved")
    monkeypatch.setenv("CATALYST_PROVIDER", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    captured = {}
    db, _digest = _make_runtime_db(tmp_path, name="q001-derivative.db")
    adapter = module.build_operator_runner_adapter(
        data_db_path=db,
        runtime_db_path=tmp_path / "runtime.sqlite3",
        composition_builder=lambda **kwargs: object(),
        dependency_loader_builder=lambda **kwargs: object(),
        adapter_factory=lambda **kwargs: captured.update(kwargs) or kwargs,
    )
    with pytest.raises(module.Stage1OperatorError, match="credential|configured|missing"):
        adapter["credential_provider"]()


def test_production_factory_rejects_case_timeout_shorter_than_run_deadline(
    tmp_path, monkeypatch
):
    module = _load_cli()
    for name in module._OPERATOR_REQUIRED_ENV:
        monkeypatch.setenv(name, "approved")
    db, _digest = _make_runtime_db(tmp_path, name="q001-derivative.db")
    with pytest.raises(module.Stage1OperatorError, match="timeout|deadline"):
        module.build_operator_runner_adapter(
            data_db_path=db,
            runtime_db_path=tmp_path / "runtime.sqlite3",
            case_timeout_seconds=59.0,
            composition_builder=lambda **kwargs: object(),
            dependency_loader_builder=lambda **kwargs: object(),
            adapter_factory=lambda **kwargs: kwargs,
        )


# ---------------------------------------------------------------------------
# Phase A: immutable data DB vs writable runtime DB + execute authorization
# ---------------------------------------------------------------------------

def _fake_execute_factory(*, calls: list[str] | None = None):
    """Compliant fake runner adapter (no provider): one outcome per case.

    It consumes the shared provider budget guard exactly like a real dispatch
    so the CLI's accounting reconciliation sees no gap.
    """
    from catalyst_eval.v1_1.loader import canonical_bytes
    from catalyst_eval.v1_1.runner import CaseRunOutcome

    calls = [] if calls is None else calls
    budget_holder: dict = {}

    def run_case(case):
        budget = budget_holder.get("guard")
        charged_cost = None
        if budget is not None:
            reservation = budget.reserve(
                role="evidence_analyst", provider="deepseek", model_id="deepseek-chat"
            )
            budget.settle(reservation)
            charged_cost = budget.case_snapshot()["case_used_cost_usd"]
        calls.append(case.case_id)
        manifest_payload = {"run_id": f"manifest:{case.case_id}", "case_id": case.case_id}
        result_payload = {"result": case.case_id}
        return CaseRunOutcome(
            case_id=case.case_id,
            run_manifest_id=f"manifest:{case.case_id}",
            run_manifest_hash=hashlib.sha256(
                canonical_bytes(manifest_payload)
            ).hexdigest(),
            result_artifact_id=f"result:{case.case_id}",
            result_artifact_hash=hashlib.sha256(
                canonical_bytes(result_payload)
            ).hexdigest(),
            terminal_status="COMPLETED",
            run_manifest_payload=manifest_payload,
            result_artifact_payload=result_payload,
            context_pack_ref={
                "artifact_id": f"pack:{case.case_id}",
                "artifact_hash": "a" * 64,
            },
            claim_plan_ref={
                "artifact_id": f"claimplan:{case.case_id}",
                "artifact_hash": "b" * 64,
            },
            assurance_ref={
                "artifact_id": f"assurance:{case.case_id}",
                "artifact_hash": "c" * 64,
            },
            provider_calls=1,
            cost_usd=charged_cost,
            run_facts={
                "schema_version": "v1_1_stage1_run_facts_v1",
                "case_id": case.case_id,
                "output_status": case.oracle_status,
                "attribution_type": case.expected_attribution_type
                or "EVIDENCE_BACKED_CAUSAL",
                "refusal_reason": case.expected_refusal_reason,
                "refusal_reason_available": True,
                "claims": [],
                "sanity_tasks_completed": [],
                "latency_ms": None,
                "tokens": None,
                "cost_usd": charged_cost,
                "model_limited": False,
                "trajectory": {
                    "corrective_triggered": False,
                    "rounds_executed": 0,
                    "gap_reason_codes": [],
                    "corrective_actions": [],
                    "research_fingerprints": [],
                    "evidence_delta_ids": [],
                    "produced_structure": False,
                },
                "retrieval": {
                    "observed": False,
                    "pool": None,
                    "ranked_evidence_ids": [],
                    "candidate_evidence_ids": [],
                    "reranker_contributed": False,
                    "latency_ms": None,
                    "degraded": False,
                    "ticker_violations": [],
                    "cutoff_violations": [],
                },
                "provider_accounting": {
                    "provider_calls": 1,
                    "tokens_in": None,
                    "tokens_out": None,
                    "cost_usd": charged_cost,
                    "cost_method": (
                        "upper_bound_charged"
                        if charged_cost is not None
                        else "unavailable"
                    ),
                },
            },
        )

    def factory(**kwargs):
        budget_holder["guard"] = kwargs.get("provider_budget")
        return run_case

    return factory, calls


def _prepare_cli_env(tmp_path, module, *, ref="v1:corpus:q011"):
    rows = make_stage1_cases()
    strat = make_stratification(rows)
    db, digest = _make_runtime_db(tmp_path)
    manifest = _manifest_with_identity(rows, strat, ref=ref, digest=digest)
    _cases, manifest_path = _write_legacy_dataset(tmp_path, rows, manifest)
    return db, digest, manifest_path, tmp_path / "v1_1_stage1_stratification.json"


def _run_prepare(module, manifest_path, strat_path, out_dir, db, calls, cost):
    return module.main([
        "prepare",
        "--dataset-manifest", str(manifest_path),
        "--stratification", str(strat_path),
        "--output-dir", str(out_dir),
        "--max-provider-calls", str(calls),
        "--max-cost-usd", str(cost),
        "--runtime-db", str(db),
    ])


def test_prepare_declares_output_owned_writable_runtime_db(tmp_path):
    module = _load_cli()
    db, _digest, manifest_path, strat_path = _prepare_cli_env(tmp_path, module)
    out_dir = tmp_path / "out"
    assert _run_prepare(module, manifest_path, strat_path, out_dir, db, 100, 10.0) == 0
    summary = json.loads((out_dir / "prepare_summary.json").read_text(encoding="utf-8"))
    write_db = Path(summary["runtime_write_db_path"])
    assert write_db.name == "runtime.sqlite3"
    assert write_db.parent == out_dir.resolve()
    assert write_db != Path(summary["runtime_db_path"])
    # prepare never creates the writable runtime DB.
    assert not write_db.exists()


def test_execute_rehashes_data_db_and_rejects_drift(tmp_path):
    module = _load_cli()
    db, _digest, manifest_path, strat_path = _prepare_cli_env(tmp_path, module)
    out_dir = tmp_path / "out"
    assert _run_prepare(module, manifest_path, strat_path, out_dir, db, 100, 10.0) == 0
    # Drift the immutable Q-001 derivative after prepare.
    db.write_bytes(db.read_bytes() + b"drift")
    factory, calls = _fake_execute_factory()
    exit_code = module.main(
        ["execute", "--output-dir", str(out_dir),
         "--max-provider-calls", "100", "--max-cost-usd", "10.0",
         "--pricing-json", str(_write_pricing(tmp_path))],
        runner_adapter_factory=factory,
    )
    assert calls == [], "DB drift must stop before any dispatch"
    assert exit_code == 2
    assert not (out_dir / "execution_ledger.jsonl").exists()


def test_execute_rejects_ceiling_mismatch_with_prepare(tmp_path):
    module = _load_cli()
    for exec_calls, exec_cost in ((101, 10.0), (100, 9.0)):
        db, _digest, manifest_path, strat_path = _prepare_cli_env(tmp_path, module)
        out_dir = tmp_path / "out"
        assert _run_prepare(module, manifest_path, strat_path, out_dir, db, 100, 10.0) == 0
        factory, calls = _fake_execute_factory()
        exit_code = module.main(
            ["execute", "--output-dir", str(out_dir),
             "--max-provider-calls", str(exec_calls),
             "--max-cost-usd", str(exec_cost)],
            runner_adapter_factory=factory,
        )
        assert exit_code == 2
        assert calls == [], "a ceiling mismatch must stop before any dispatch"


def test_zero_prepare_ceiling_never_authorizes_positive_execution(tmp_path):
    module = _load_cli()
    db, _digest, manifest_path, strat_path = _prepare_cli_env(tmp_path, module)
    out_dir = tmp_path / "out"
    assert _run_prepare(module, manifest_path, strat_path, out_dir, db, 0, 0.0) == 0
    factory, calls = _fake_execute_factory()
    assert module.main(
        ["execute", "--output-dir", str(out_dir),
         "--max-provider-calls", "1", "--max-cost-usd", "1.0"],
        runner_adapter_factory=factory,
    ) == 2
    assert calls == []


def test_execute_and_prepare_leave_canonical_db_byte_identical(tmp_path):
    module = _load_cli()
    db, _digest, manifest_path, strat_path = _prepare_cli_env(tmp_path, module)
    before = db.read_bytes()
    out_dir = tmp_path / "out"
    assert _run_prepare(module, manifest_path, strat_path, out_dir, db, 100, 10.0) == 0
    assert db.read_bytes() == before, "prepare mutated the immutable Q-001 DB"
    factory, calls = _fake_execute_factory()
    assert module.main(
        ["execute", "--output-dir", str(out_dir),
         "--max-provider-calls", "100", "--max-cost-usd", "10.0",
         "--pricing-json", str(_write_pricing(tmp_path))],
        runner_adapter_factory=factory,
    ) == 0
    assert calls, "execute should have dispatched the case set"
    assert db.read_bytes() == before, "execute mutated the immutable Q-001 DB"
    for suffix in ("-wal", "-shm", "-journal"):
        assert not db.with_name(db.name + suffix).exists(), (
            f"execute created a SQLite side file {suffix} next to the Q-001 DB"
        )


def test_streamed_sha256_hashes_large_file_in_bounded_chunks(tmp_path, monkeypatch):
    module = _load_cli()
    from catalyst_eval.v1_1.loader import (
        RUNTIME_DB_HASH_CHUNK_BYTES,
        streamed_sha256,
    )

    payload = b"q" * (RUNTIME_DB_HASH_CHUNK_BYTES * 3 + 17)
    target = tmp_path / "large-runtime.db"
    target.write_bytes(payload)

    original_open = io.open
    target_resolved = target.resolve()
    read_sizes: list[int] = []

    class _CountingReader:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *exc):
            return self._handle.__exit__(*exc)

        def read(self, *args, **kwargs):
            data = self._handle.read(*args, **kwargs)
            read_sizes.append(len(data))
            return data

        def __getattr__(self, name):
            return getattr(self._handle, name)

    def counting_open(file, *args, **kwargs):
        handle = original_open(file, *args, **kwargs)
        try:
            resolved = Path(file).resolve()
        except (TypeError, ValueError):
            return handle
        return _CountingReader(handle) if resolved == target_resolved else handle

    monkeypatch.setattr(io, "open", counting_open)
    digest = streamed_sha256(target)
    assert digest == hashlib.sha256(payload).hexdigest()
    # More than one bounded read: the file spans multiple configured chunks.
    assert len(read_sizes) >= 4, read_sizes
    assert max(read_sizes) <= RUNTIME_DB_HASH_CHUNK_BYTES, read_sizes
