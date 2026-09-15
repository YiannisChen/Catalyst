#!/usr/bin/env python3
"""V1.1 Stage-1 runner CLI (M7-8, Batch-B corrective).

Usage:
  run_v1_1_stage1.py {prepare,execute,audit,report,all} [options]

- ``prepare`` performs NO provider calls; it loads the dataset manifest and
  the stratification input (``--stratification``, else the manifest block,
  else the sibling ``stratification.json``/legacy file), builds/verifies the
  actual EvalManifest and eval_id, requires ``--runtime-db`` (hashed
  incrementally) whose SHA-256 must equal the declared Q-001 file SHA,
  verifies the execution HEAD, and records the operator-approved
  provider/cost ceilings plus the mandatory Q-002 NON-COMPARABLE marker.
  Exit 2 means stop.

New V1.1 surfaces use professional benchmark terminology: the case file is
``cases.jsonl`` and the dataset manifest may declare schema
``v1_1_benchmark_dataset_manifest_v1``; the sealed legacy
``v1_1_stage1_*`` names and ``v1_1_stage1_dataset_manifest_v1`` stay
readable for compatibility.
- ``execute`` requires explicit ``--max-provider-calls`` and
  ``--max-cost-usd`` that must EQUAL the prepared ceilings exactly (a 0/0
  prepare never authorizes later positive execution), incrementally re-hashes
  the immutable Q-001 derivative and requires byte-identical equality with the
  prepare summary, refuses an incompatible ledger, resumes only identity-valid
  COMPLETED rows (FAILED/CANCELLED/corrupt/nonterminal/identity-mismatched rows
  are never completed success), enforces the provider-call and cost ceilings
  before every dispatch (binding the remaining budget + prepared identity +
  case timeout into the production adapter), and preserves resumable evidence
  on stop. The live M6 app/SSE adapter is injectable.

The immutable Q-001 derivative (``--runtime-db``) and the writable M6 runtime
database (``<output-dir>/runtime.sqlite3``) are separate files: the CLI never
initializes, WAL-enables, migrates, or admits runs into Q-001.
- ``audit`` runs full validation of the human-provided audit file against
  the ledger/run artifacts; it can never author human decisions.
- ``report`` joins only sealed run artifacts and the sealed human audit,
  computes real retrieval/attribution/trajectory gates, and publishes
  write-once canonical JSON plus deterministic Markdown. It never
  fabricates empty gates or counts.
- ``all`` = prepare -> execute -> audit -> report.

Exit codes: 0 success, 1 gate failure, 2 contract/operator/configuration
error.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from catalyst_eval.v1_1.execution_ledger import ExecutionLedger, LedgerRow
from catalyst_eval.v1_1.loader import (
    STAGE1_MANIFEST_SCHEMAS,
    dataset_content_sha256,
    load_benchmark_cases,
    resolve_benchmark_cases_path,
    resolve_benchmark_stratification_path,
    streamed_sha256,
    validate_stage1_dataset_manifest,
)
from catalyst_eval.v1_1.manifest import (
    AgentPolicyIdentity,
    CodeProviderIdentity,
    DataRuntimeIdentityReference,
    EvalManifest,
    MetricContract,
    RetrievalPolicyIdentity,
)
from catalyst_eval.v1_1.manifest_builder import (
    EligibleExperimentInput,
    Stage1DatasetInput,
    build_eval_manifest,
)
from catalyst_eval.v1_1.output_audit import (
    load_output_audit,
    validate_audit_against_ledger,
)
from catalyst_eval.v1_1.report import (
    build_report_payload,
    render_report_markdown,
    scan_report_for_secrets,
    write_report_json,
    write_report_markdown,
)
from catalyst_eval.v1_1.runner import (
    CaseRunOutcome,
    RunArtifactHashError,
    _verify_artifact_hashes,
)

EXIT_OK = 0
EXIT_GATE_FAILURE = 1
EXIT_CONTRACT_ERROR = 2

PREPARE_SUMMARY_SCHEMA = "v1_1_stage1_prepare_summary_v1"
LEDGER_FILENAME = "execution_ledger.jsonl"
PREPARE_SUMMARY_FILENAME = "prepare_summary.json"
# The M6 runtime writes (runs/events/artifacts) live in an output-dir-owned
# writable database, separate from the immutable Q-001 data derivative.
RUNTIME_WRITE_DB_FILENAME = "runtime.sqlite3"
Q002_REASON = "q_002_promoted_environment_tuple_unrecovered"

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class Stage1OperatorError(RuntimeError):
    pass


class Stage1CeilingExceeded(Stage1OperatorError):
    pass


class Stage1GateFailure(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Injectability: the live M6 app/SSE adapter is supplied through
# ``main(..., runner_adapter_factory=...)``; the production default is the
# M7-10 operator gate and never invents provider dispatch.
# ---------------------------------------------------------------------------

# Required production operator configuration. When any is absent the factory
# fails closed before it builds a runtime; it never invents a runtime identity
# or a provider dispatch.
_OPERATOR_REQUIRED_ENV = (
    "CATALYST_LANCEDB_DIR",
    "CATALYST_INDEX_MANIFEST_PATH",
    "CATALYST_CORPUS_MANIFEST_ID",
    "CATALYST_INDEX_MANIFEST_ID",
    "CATALYST_SOURCE_BUNDLE_ID",
    "CATALYST_SNAPSHOT_ID",
    "CATALYST_PROBE_REPORT_ID",
    "CATALYST_POSTBUILD_READINESS_ID",
)


def build_operator_runner_adapter(
    *,
    data_db_path: str | Path,
    runtime_db_path: str | Path,
    prepared_identity_ref: str | None = None,
    prepared_identity_hash: str | None = None,
    case_timeout_seconds: float | None = None,
    provider_budget: Any = None,
    composition_builder: Callable[..., Any] | None = None,
    dependency_loader_builder: Callable[..., Any] | None = None,
    adapter_factory: Callable[..., Any] | None = None,
) -> Any:
    """Build the live M6 app/SSE operator adapter (production wiring).

    Two databases, never one:

    * ``data_db_path`` is the immutable Q-001 operational derivative. It is
      only ever handed to ``RuntimeDependencyLoader``, which reads it through
      the data-core read-only factory; it is never initialized, WAL-enabled,
      migrated, or admitted into.
    * ``runtime_db_path`` is the writable M6 runtime database owned by the
      output directory (``runtime.sqlite3``). The composition/event repository
      creates/uses it for runs, events, and artifacts.

    Missing runtime configuration fails closed before any runtime is built;
    no provider call happens at construction.
    """
    resolved_data_db = Path(data_db_path)
    resolved_runtime_db = Path(runtime_db_path)
    if not resolved_data_db.is_file():
        raise Stage1OperatorError(f"data runtime DB not found: {resolved_data_db}")
    if resolved_data_db.resolve() == resolved_runtime_db.resolve():
        raise Stage1OperatorError(
            "the immutable Q-001 data DB and the writable M6 runtime DB must be "
            "separate files; refusing to initialize/WAL/migrate the data DB"
        )
    from catalyst_agents.runtime.manifest import INITIAL_RUN_TIMEOUT_SECONDS

    effective_case_timeout = (
        case_timeout_seconds
        if case_timeout_seconds is not None
        else float(INITIAL_RUN_TIMEOUT_SECONDS)
    )
    if effective_case_timeout < INITIAL_RUN_TIMEOUT_SECONDS:
        raise Stage1OperatorError(
            "case timeout must be at least the production RunManifest deadline "
            f"({INITIAL_RUN_TIMEOUT_SECONDS}s)"
        )
    missing = [name for name in _OPERATOR_REQUIRED_ENV if not os.environ.get(name, "").strip()]
    if missing:
        raise Stage1OperatorError(
            "operator configuration is incomplete; refusing to build the M6 "
            f"app/SSE adapter (missing: {', '.join(missing)})"
        )

    build_composition = composition_builder
    if build_composition is None:
        from catalyst_app.runtime.composition import build_runtime_composition

        build_composition = build_runtime_composition
    build_loader = dependency_loader_builder
    if build_loader is None:
        from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
        from catalyst_agents.runtime.query_embedding import (
            ProductionBgeM3QueryEmbeddingFactory,
        )

        def build_loader(*, sqlite_db_path: str | Path):  # type: ignore[misc]
            manifest_path = os.environ.get("CATALYST_INDEX_MANIFEST_PATH", "").strip()
            return RuntimeDependencyLoader(
                sqlite_db_path=sqlite_db_path,
                require_identity_bound_runtime=True,
                query_embedding_factory=ProductionBgeM3QueryEmbeddingFactory(),
                index_manifest_path=(Path(manifest_path) if manifest_path else None),
            )
    make_adapter = adapter_factory
    if make_adapter is None:
        from catalyst_eval.v1_1.m6_adapter import M6AppSseRunnerAdapter

        make_adapter = M6AppSseRunnerAdapter

    dependency_loader = build_loader(sqlite_db_path=resolved_data_db)
    composition = build_composition(
        db_path=resolved_runtime_db,
        dependency_loader=dependency_loader,
        provider_budget=provider_budget,
    )

    def _resolve_provider_credential() -> str:
        """Resolve the operator's provider key WITHOUT printing or storing it.

        The value is returned to the adapter and registered only through the
        admission pre-submit hook; it never enters an artifact, event, log, or
        exception.
        """
        from catalyst_app.env_loader import get_provider_env_key

        provider = os.environ.get("CATALYST_PROVIDER", "deepseek").strip()
        env_name = get_provider_env_key(provider)
        if not env_name:
            raise Stage1OperatorError(
                "provider credential env name is unavailable; refusing dispatch"
            )
        env_value = os.environ.get(env_name, "").strip()
        if not env_value:
            raise Stage1OperatorError(
                "provider credential is missing; refusing dispatch"
            )
        return env_value

    return make_adapter(
        composition=composition,
        db_path=resolved_runtime_db,
        provider=os.environ.get("CATALYST_PROVIDER", "deepseek").strip(),
        model_id=os.environ.get("CATALYST_MODEL_ID", "deepseek-flash").strip(),
        prepared_identity_ref=prepared_identity_ref,
        prepared_identity_hash=prepared_identity_hash,
        case_timeout_seconds=(
            effective_case_timeout
        ),
        credential_provider=_resolve_provider_credential,
    )


def _default_runner_adapter_factory(
    *,
    data_db_path: str | Path | None = None,
    runtime_db_path: str | Path | None = None,
    prepared_identity_ref: str | None = None,
    prepared_identity_hash: str | None = None,
    case_timeout_seconds: float | None = None,
    provider_budget: Any = None,
) -> Any:
    if data_db_path is None or runtime_db_path is None:
        raise Stage1OperatorError(
            "live Stage-1 execution requires both the immutable --runtime-db "
            "(Q-001 derivative) and a writable output-dir runtime database"
        )
    return build_operator_runner_adapter(
        data_db_path=data_db_path,
        runtime_db_path=runtime_db_path,
        prepared_identity_ref=prepared_identity_ref,
        prepared_identity_hash=prepared_identity_hash,
        case_timeout_seconds=case_timeout_seconds,
        provider_budget=provider_budget,
    )


def _build_adapter(
    *,
    data_db_path: str | Path,
    runtime_db_path: str | Path,
    prepared_identity_ref: str | None,
    prepared_identity_hash: str | None,
    case_timeout_seconds: float | None,
    provider_budget: Any,
    runner_adapter_factory: Callable[..., Any] | None,
) -> Any:
    """Invoke the runner adapter factory with the accepted operator bindings.

    The production factory and the legacy zero-argument test factories stay
    compatible: only the keyword arguments the factory actually accepts (or a
    ``**kwargs`` catch-all) are passed.
    """
    factory = runner_adapter_factory or _default_runner_adapter_factory
    candidates = {
        "db_path": runtime_db_path,
        "data_db_path": data_db_path,
        "runtime_db_path": runtime_db_path,
        "prepared_identity_ref": prepared_identity_ref,
        "prepared_identity_hash": prepared_identity_hash,
        "case_timeout_seconds": case_timeout_seconds,
        "provider_budget": provider_budget,
    }
    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_var_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if accepts_var_kwargs:
        return factory(**candidates)
    accepted = {
        name: value for name, value in candidates.items() if name in parameters
    }
    if accepted:
        return factory(**accepted)
    return factory()


def _invoke_adapter(adapter: Any, case: Any) -> CaseRunOutcome:
    run_case = getattr(adapter, "run_case", None)
    if callable(run_case):
        return run_case(case)
    if callable(adapter):
        return adapter(case)
    raise Stage1OperatorError(
        "runner adapter must expose run_case(case) or be callable"
    )


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def _execution_head_sha256() -> str:
    repo_root = Path(__file__).resolve().parents[3]
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise Stage1OperatorError("cannot resolve execution HEAD (not a git repo?)")
    return proc.stdout.strip()


def _execution_head_sha8() -> str:
    return _execution_head_sha256()[:8]


def _load_dataset_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise Stage1OperatorError("dataset manifest must be a JSON object")
    return manifest


def _load_prepare_summary(output_dir: Path) -> dict[str, Any]:
    path = output_dir / PREPARE_SUMMARY_FILENAME
    if not path.is_file():
        raise Stage1OperatorError(
            f"prepare summary missing at {path}; run prepare first"
        )
    summary = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        raise Stage1OperatorError("prepare summary must be a JSON object")
    if summary.get("schema_version") != PREPARE_SUMMARY_SCHEMA:
        raise Stage1OperatorError(
            f"prepare summary schema must be {PREPARE_SUMMARY_SCHEMA}"
        )
    return summary


def _load_eval_manifest_and_cases(summary: dict[str, Any], output_dir: Path):
    manifest = EvalManifest.model_validate(summary["eval_manifest"])
    dataset_manifest_path = Path(summary["dataset_manifest_path"])
    dataset_manifest = _load_dataset_manifest(dataset_manifest_path)
    cases = load_benchmark_cases(
        resolve_benchmark_cases_path(dataset_manifest, dataset_manifest_path),
        manifest=dataset_manifest,
    )
    return manifest, cases


def _load_stratification(
    dataset_manifest: Mapping[str, Any],
    dataset_manifest_path: Path,
    stratification: Path | None,
) -> tuple[Mapping[str, Any], Path | None]:
    """Resolve the stratification input: explicit flag, else the manifest block,
    else the benchmark/legacy sibling file. Never fabricate strata.

    Returns the validated payload plus the resolved on-disk path (``None`` when
    the stratification is embedded in the manifest), so ``prepare``/``report``
    can reference the same input the operator supplied.
    """
    if stratification is not None:
        payload = json.loads(stratification.read_text(encoding="utf-8"))
        resolved = stratification.resolve()
    elif isinstance(dataset_manifest.get("stratification"), Mapping):
        return dataset_manifest["stratification"], None
    else:
        sibling = resolve_benchmark_stratification_path(
            dataset_manifest, dataset_manifest_path
        )
        if sibling is None:
            raise Stage1OperatorError(
                "prepare requires stratification: pass --stratification or "
                "embed a stratification block in the dataset manifest"
            )
        payload = json.loads(sibling.read_text(encoding="utf-8"))
        resolved = sibling.resolve()
    if not isinstance(payload, dict):
        raise Stage1OperatorError("stratification file must be a JSON object")
    return payload, resolved


def _resolve_runtime_identity(
    manifest: Mapping[str, Any],
    *,
    declaration_ref: str | None,
    declaration_hash: str | None,
) -> tuple[str, str]:
    """Resolve the DataRuntimeIdentity object binding without any fallback.

    The declaration comes from the Q-001 operational derivative evidence: the
    explicit ``--runtime-identity-ref``/``--runtime-identity-hash`` operator
    flags, else the dataset manifest fields. Missing either one fails closed;
    the ``runtime-id:stage1`` / ``d``*64 fallbacks are removed.
    """
    ref = (declaration_ref or manifest.get("data_runtime_identity_ref") or "").strip()
    declared_hash = (
        declaration_hash or manifest.get("data_runtime_identity_hash") or ""
    ).strip()
    if not ref or not declared_hash:
        raise Stage1OperatorError(
            "DataRuntimeIdentity object identity is undeclared; pass --runtime-identity-ref "
            "and --runtime-identity-hash or record data_runtime_identity_ref/"
            "data_runtime_identity_hash in the dataset manifest"
        )
    if _SHA256_RE.fullmatch(declared_hash) is None:
        raise Stage1OperatorError(
            "DataRuntimeIdentity object hash must be a SHA-256 hex digest"
        )
    return ref, declared_hash


def _verify_runtime_identity(
    *,
    declared_ref: str,
    declared_hash: str,
)-> None:
    """Validate the DataRuntimeIdentity object binding only.

    This hash is not the Q-001 file digest. The two identities are resolved
    and verified independently before provider dispatch.
    """
    if not declared_ref or not declared_hash:
        raise Stage1OperatorError(
            "Q-001 runtime identity ref/hash are required; exact operational "
            "derivative identity must be declared before live execution"
        )
    if _SHA256_RE.fullmatch(declared_hash) is None:
        raise Stage1OperatorError(
            "DataRuntimeIdentity object hash must be a SHA-256 hex digest"
        )
    return None


def _verify_q001_file_identity(
    *, runtime_db: Path, declared_file_sha256: str
) -> tuple[int, str]:
    """Verify the immutable Q-001 file SHA independently of object identity."""
    if _SHA256_RE.fullmatch(declared_file_sha256) is None:
        raise Stage1OperatorError("Q-001 file SHA-256 must be a lowercase hex digest")
    runtime_db = Path(runtime_db)
    if not runtime_db.is_file():
        raise Stage1OperatorError(f"Q-001 runtime DB not found: {runtime_db}")
    actual = streamed_sha256(runtime_db)
    if actual != declared_file_sha256:
        raise Stage1OperatorError(
            "Q-001 runtime DB file SHA-256 does not match its declared file "
            f"identity: declared={declared_file_sha256} actual={actual}"
        )
    return runtime_db.stat().st_size, actual


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def _prepare(
    *,
    dataset_manifest: Path | None,
    stratification: Path | None,
    output_dir: Path,
    max_provider_calls: int | None,
    max_cost_usd: float | None,
    runtime_db: Path | None,
    runtime_identity_ref: str | None = None,
    runtime_identity_hash: str | None = None,
    q001_file_sha256: str | None = None,
    **__,
) -> int:
    if dataset_manifest is None:
        raise Stage1OperatorError("prepare requires --dataset-manifest")
    if max_provider_calls is None or max_cost_usd is None:
        raise Stage1OperatorError(
            "prepare requires --max-provider-calls and --max-cost-usd "
            "(operator-approved ceilings)"
        )
    if runtime_db is None:
        raise Stage1OperatorError(
            "prepare requires --runtime-db (the exact Q-001 operational "
            "derivative SQLite database); it is never inferred"
        )
    manifest = _load_dataset_manifest(dataset_manifest)
    if manifest.get("schema_version") not in STAGE1_MANIFEST_SCHEMAS:
        raise Stage1OperatorError(
            f"dataset manifest schema must be one of {STAGE1_MANIFEST_SCHEMAS}"
        )
    cases = load_benchmark_cases(
        resolve_benchmark_cases_path(manifest, dataset_manifest),
        manifest=manifest,
    )
    strat_payload, stratification_path = _load_stratification(
        manifest, dataset_manifest, stratification
    )
    validate_stage1_dataset_manifest(
        manifest, cases, stratification=strat_payload
    )

    head_sha256 = _execution_head_sha256()
    declared_ref, declared_hash = _resolve_runtime_identity(
        manifest,
        declaration_ref=runtime_identity_ref,
        declaration_hash=runtime_identity_hash,
    )
    _verify_runtime_identity(
        declared_ref=declared_ref,
        declared_hash=declared_hash,
    )
    declared_file_sha256 = (
        q001_file_sha256 or manifest.get("q001_file_sha256") or ""
    ).strip()
    if not declared_file_sha256:
        raise Stage1OperatorError(
            "Q-001 file SHA is undeclared; pass --q001-file-sha256 or record "
            "q001_file_sha256 in the dataset manifest"
        )
    runtime_db_size, q001_file_sha256 = _verify_q001_file_identity(
        runtime_db=runtime_db, declared_file_sha256=declared_file_sha256
    )

    from catalyst_agents.runtime.experiment import (
        A1_PACKING_VERSIONS,
        A2_HYPOTHESIS_VERSIONS,
        A3_ROUNDS,
        A5_OBSERVATION_VERSIONS,
    )

    eligible_inputs: list[EligibleExperimentInput] = []
    a3_ids = list(manifest.get("a3_eligible_ids") or ())
    if a3_ids:
        eligible_inputs.append(
            EligibleExperimentInput(
                experiment_id="A3",
                eligibility_predicate="human-labelled-recoverable",
                ordered_eligible_ids=tuple(a3_ids),
                minimum_eligible_denominator=1,
            )
        )
    a4_ids = list(manifest.get("a4_readiness_eligible_ids") or ())
    if a4_ids:
        eligible_inputs.append(
            EligibleExperimentInput(
                experiment_id="A4",
                eligibility_predicate="multi-gap-recoverable-v1",
                ordered_eligible_ids=tuple(a4_ids),
                minimum_eligible_denominator=1,
            )
        )

    eval_manifest = build_eval_manifest(
        dataset=Stage1DatasetInput(
            dataset_id=str(manifest.get("dataset_id") or "stage1"),
            dataset_version=str(manifest.get("dataset_version") or "1.1.0"),
            cases=tuple(cases),
        ),
        stage="stage1",
        split="dev",
        code_identity=CodeProviderIdentity(
            code_git_sha=head_sha256,
            harness_revision="v1.1-stage1",
            random_seed=0,
        ),
        data_runtime_identity=DataRuntimeIdentityReference(
            data_runtime_identity_ref=declared_ref,
            data_runtime_identity_hash=declared_hash,
        ),
        agent_policy=AgentPolicyIdentity(
            observation_policy_version="move_profile_v1",
            context_pack_policy_version="evidence_context_pack_v1",
            analyst_policy_version="bounded_competition_v1",
            writer_policy_version="writer_v1",
            a1_policy_version=sorted(A1_PACKING_VERSIONS)[0],
            a2_policy_version=sorted(A2_HYPOTHESIS_VERSIONS)[0],
            a3_policy_version=str(A3_ROUNDS[0]),
            a4_policy_version="multi-gap-recoverable-v1",
            a5_policy_version=sorted(A5_OBSERVATION_VERSIONS)[0],
        ),
        retrieval_policy=RetrievalPolicyIdentity(
            arm_names=("fts5", "dense", "hybrid", "reranked"),
            arm_order=("fts5", "dense", "hybrid", "reranked"),
            top_k=8,
            dedup_policy_version="dedup:v1",
            independence_policy_version="ind:v1",
            reranker_policy_version="rr:v1",
        ),
        metric_spec=MetricContract(metric_spec_version="ms:v1", definitions=()),
        eligible_experiments=eligible_inputs,
    )

    summary = {
        "schema_version": PREPARE_SUMMARY_SCHEMA,
        "eval_id": eval_manifest.evaluation_identity.eval_id,
        "eval_manifest": eval_manifest.model_dump(mode="json"),
        "dataset_manifest_path": str(dataset_manifest.resolve()),
        "stratification_path": (
            str(stratification_path) if stratification_path is not None else None
        ),
        "dataset_id": manifest.get("dataset_id"),
        "dataset_version": manifest.get("dataset_version"),
        "dataset_content_sha256": dataset_content_sha256(cases),
        "case_count": len(cases),
        "execution_head_sha256": head_sha256,
        "execution_head_sha8": head_sha256[:8],
        "max_provider_calls": max_provider_calls,
        "max_cost_usd": max_cost_usd,
        "data_runtime_identity_ref": declared_ref,
        "data_runtime_identity_object_hash": declared_hash,
        "q001_file_sha256": q001_file_sha256,
        "case_timeout_seconds": 60,
        "runtime_db_path": str(Path(runtime_db).resolve()),
        "runtime_db_size_bytes": runtime_db_size,
        "runtime_write_db_path": str(
            (output_dir / RUNTIME_WRITE_DB_FILENAME).resolve()
        ),
        "comparability": "NON-COMPARABLE",
        "comparability_reason": Q002_REASON,
        "ok": True,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / PREPARE_SUMMARY_FILENAME).write_text(
        json.dumps(summary, sort_keys=True, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, sort_keys=True))
    return EXIT_OK


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

def _ledger_row_from_outcome(
    outcome: CaseRunOutcome, *, eval_id: str, attempts: int
) -> LedgerRow:
    if outcome.terminal_status == "COMPLETED":
        from catalyst_eval.v1_1.run_facts import validate_run_facts

        validate_run_facts(
            outcome.run_facts or {},
            expected_case_id=outcome.case_id,
            row_provider_calls=outcome.provider_calls,
        )
    return LedgerRow(
        eval_id=eval_id,
        case_id=outcome.case_id,
        run_manifest_id=outcome.run_manifest_id,
        run_manifest_hash=outcome.run_manifest_hash,
        result_artifact_id=outcome.result_artifact_id,
        result_artifact_hash=outcome.result_artifact_hash,
        terminal_status=outcome.terminal_status,
        attempts=attempts,
        provider_calls=outcome.provider_calls,
        cost_usd=outcome.cost_usd,
        checksum="",
        run_facts=outcome.run_facts,
        context_pack_ref=(
            dict(outcome.context_pack_ref) if outcome.context_pack_ref else None
        ),
        claim_plan_ref=(
            dict(outcome.claim_plan_ref) if outcome.claim_plan_ref else None
        ),
        assurance_ref=(
            dict(outcome.assurance_ref) if outcome.assurance_ref else None
        ),
    )


def _verify_execute_authorization(
    summary: Mapping[str, Any],
    *,
    max_provider_calls: int,
    max_cost_usd: float,
) -> tuple[Path, Path]:
    """Authorize execute from the sealed prepare summary.

    Fail-closed requirements (Phase A corrective):

    * the immutable Q-001 derivative is re-hashed incrementally and must be
      byte-identical to the prepare summary (no drift between prepare/execute);
    * the execute ceilings must equal the prepared ceilings exactly;
    * a prepared 0/0 (or negative) ceiling never authorizes positive execution.

    Returns ``(data_db_path, runtime_write_db_path)``.
    """
    prepared_calls = summary.get("max_provider_calls")
    prepared_cost = summary.get("max_cost_usd")
    if prepared_calls is None or prepared_cost is None:
        raise Stage1OperatorError(
            "prepare summary records no operator ceilings; re-run prepare"
        )
    if prepared_calls <= 0 or prepared_cost <= 0:
        raise Stage1OperatorError(
            "the prepared ceilings are zero/negative; a 0/0 prepare never "
            "authorizes later positive execution"
        )
    if max_provider_calls != prepared_calls or max_cost_usd != prepared_cost:
        raise Stage1OperatorError(
            "execute ceilings must equal the prepared ceilings exactly: "
            f"prepared=({prepared_calls}, {prepared_cost}) "
            f"execute=({max_provider_calls}, {max_cost_usd})"
        )

    prepared_identity_ref = summary.get("data_runtime_identity_ref")
    prepared_identity_hash = summary.get("data_runtime_identity_object_hash")
    if not prepared_identity_ref or not prepared_identity_hash:
        raise Stage1OperatorError(
            "prepare summary has no DataRuntimeIdentity object binding; re-run prepare"
        )
    _verify_runtime_identity(
        declared_ref=str(prepared_identity_ref),
        declared_hash=str(prepared_identity_hash),
    )

    data_db_path = summary.get("runtime_db_path")
    if not data_db_path:
        raise Stage1OperatorError(
            "prepare summary has no runtime_db_path; re-run prepare with "
            "--runtime-db before execute"
        )
    data_db = Path(data_db_path)
    if not data_db.is_file():
        raise Stage1OperatorError(f"Q-001 runtime DB not found: {data_db}")
    declared_hash = summary.get("q001_file_sha256")
    if not declared_hash:
        raise Stage1OperatorError(
            "prepare summary has no q001_file_sha256; re-run prepare"
        )
    declared_size = summary.get("runtime_db_size_bytes")
    actual_size = data_db.stat().st_size
    if declared_size is not None and actual_size != declared_size:
        raise Stage1OperatorError(
            "Q-001 runtime DB size drifted since prepare: "
            f"prepared={declared_size} actual={actual_size}"
        )
    actual_hash = streamed_sha256(data_db)
    if actual_hash != declared_hash:
        raise Stage1OperatorError(
            "Q-001 runtime DB SHA-256 drifted since prepare: "
            f"prepared={declared_hash} actual={actual_hash}"
        )

    runtime_write_db_path = summary.get("runtime_write_db_path")
    if not runtime_write_db_path:
        raise Stage1OperatorError(
            "prepare summary has no runtime_write_db_path; re-run prepare with "
            "this CLI so the writable M6 runtime DB is declared"
        )
    if Path(runtime_write_db_path).resolve() == data_db.resolve():
        raise Stage1OperatorError(
            "the writable M6 runtime DB must not be the immutable Q-001 data DB"
        )
    return data_db, Path(runtime_write_db_path)


def _load_provider_pricing(path: Path | None) -> tuple[dict, dict]:
    """Load the operator's identity-bound model prices + role token limits.

    The file is a non-secret operator input: it contains published per-million
    token prices and the bounded token envelope per role. Without it, a
    positive USD ceiling cannot be enforced with a defensible pre-call upper
    bound, so the guard fails closed at the first provider attempt.
    """
    if path is None:
        return {}, {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise Stage1OperatorError("pricing file must be a JSON object")
    from catalyst_agents.runtime.provider_budget import ModelPrice, RoleTokenLimit

    prices: dict[tuple[str, str], Any] = {}
    for key, value in (payload.get("models") or {}).items():
        if not isinstance(value, Mapping):
            raise Stage1OperatorError(f"price entry {key!r} must be an object")
        provider, _, model_id = str(key).partition("/")
        if not provider or not model_id:
            raise Stage1OperatorError(
                f"price key {key!r} must be '<provider>/<model_id>'"
            )
        prices[(provider, model_id)] = ModelPrice(
            provider=provider,
            model_id=model_id,
            input_usd_per_million_tokens=float(
                value["input_usd_per_million_tokens"]
            ),
            output_usd_per_million_tokens=float(
                value["output_usd_per_million_tokens"]
            ),
        )
    limits: dict[str, Any] = {}
    for role, value in (payload.get("role_token_limits") or {}).items():
        if not isinstance(value, Mapping):
            raise Stage1OperatorError(f"role limit {role!r} must be an object")
        limits[str(role)] = RoleTokenLimit(
            max_input_tokens=int(value["max_input_tokens"]),
            max_output_tokens=int(value["max_output_tokens"]),
        )
    return prices, limits


def _build_provider_budget(
    *,
    max_provider_calls: int,
    max_cost_usd: float,
    pricing: Path | None,
) -> Any:
    """Build the shared provider budget guard for one execute run."""
    from catalyst_agents.runtime.provider_budget import ProviderBudgetGuard

    if max_cost_usd <= 0:
        raise Stage1OperatorError(
            "--max-cost-usd must be positive; zero/unknown cost ceilings "
            "cannot authorize provider dispatch"
        )
    prices, role_limits = _load_provider_pricing(pricing)
    if not prices:
        raise Stage1OperatorError(
            "a positive --max-cost-usd requires --pricing-json with "
            "identity-bound model prices; refusing to dispatch cost-bounded "
            "provider calls without a defensible pre-call upper bound"
        )
    return ProviderBudgetGuard(
        max_provider_calls=max_provider_calls,
        max_cost_usd=max_cost_usd,
        prices=prices,
        role_token_limits=role_limits,
    )


def _reconcile_case_accounting(provider_budget: Any, outcome: CaseRunOutcome) -> None:
    """Reconcile the persisted per-case accounting with the shared guard."""
    if provider_budget is None:
        return
    try:
        provider_budget.reconcile_case(
            provider_calls=outcome.provider_calls, cost_usd=outcome.cost_usd
        )
    except Exception as exc:
        raise Stage1OperatorError(
            f"provider accounting reconciliation failed for "
            f"{outcome.case_id!r}: {exc}"
        ) from exc


def _assert_data_db_unchanged(summary: Mapping[str, Any]) -> None:
    """Post-run proof the immutable Q-001 DB was never written."""
    data_db_path = summary.get("runtime_db_path")
    if not data_db_path:
        return
    data_db = Path(data_db_path)
    declared_hash = summary.get("q001_file_sha256")
    if not data_db.is_file():
        raise Stage1OperatorError(
            f"Q-001 runtime DB disappeared during execute: {data_db}"
        )
    actual_hash = streamed_sha256(data_db)
    if actual_hash != declared_hash:
        raise Stage1OperatorError(
            "execute mutated the immutable Q-001 runtime DB: "
            f"prepared={declared_hash} actual={actual_hash}"
        )
    for suffix in ("-wal", "-shm", "-journal"):
        stray = data_db.with_name(data_db.name + suffix)
        if stray.exists():
            raise Stage1OperatorError(
                "execute created a SQLite side file next to the immutable "
                f"Q-001 runtime DB: {stray}"
            )


def _execute(
    *,
    output_dir: Path,
    max_provider_calls: int | None,
    max_cost_usd: float | None,
    pricing: Path | None,
    runner_adapter_factory: Callable[[], Any] | None,
    **kwargs,
) -> int:
    """Run execute and always re-verify the immutable Q-001 boundary."""
    summary = _load_prepare_summary(output_dir)
    try:
        return _execute_impl(
            output_dir=output_dir,
            max_provider_calls=max_provider_calls,
            max_cost_usd=max_cost_usd,
            pricing=pricing,
            runner_adapter_factory=runner_adapter_factory,
            **kwargs,
        )
    finally:
        _assert_data_db_unchanged(summary)


def _execute_impl(
    *,
    output_dir: Path,
    max_provider_calls: int | None,
    max_cost_usd: float | None,
    pricing: Path | None,
    runner_adapter_factory: Callable[[], Any] | None,
    **__,
) -> int:
    if max_provider_calls is None or max_cost_usd is None:
        raise Stage1OperatorError(
            "execute requires explicit --max-provider-calls and --max-cost-usd"
        )
    if max_provider_calls < 0 or max_cost_usd < 0:
        raise Stage1OperatorError("ceilings must be non-negative")
    summary = _load_prepare_summary(output_dir)
    data_db, runtime_write_db_path = _verify_execute_authorization(
        summary,
        max_provider_calls=max_provider_calls,
        max_cost_usd=max_cost_usd,
    )
    eval_manifest, cases = _load_eval_manifest_and_cases(summary, output_dir)
    eval_id = eval_manifest.evaluation_identity.eval_id
    ordered_ids = list(eval_manifest.evaluation_identity.ordered_case_ids)
    gold_by_case = {case.case_id: case for case in cases}

    ledger_path = output_dir / LEDGER_FILENAME
    ledger = ExecutionLedger.load(ledger_path, expected_eval_id=eval_id)
    completed = {
        row.case_id
        for row in ledger.reusable_terminal_rows(eval_id)
        if row.terminal_status == "COMPLETED"
    }

    rows = list(ledger.rows)
    for row in rows:
        if row.eval_id == eval_id and row.cost_usd is None:
            raise Stage1OperatorError(
                f"ledger has unknown provider cost for terminal case {row.case_id!r}; "
                "refusing to treat it as $0"
            )
    used_calls = sum(
        row.provider_calls for row in rows if row.eval_id == eval_id
    )
    used_cost = sum(
        row.cost_usd for row in rows if row.eval_id == eval_id
    )

    provider_budget = _build_provider_budget(
        max_provider_calls=max_provider_calls,
        max_cost_usd=max_cost_usd,
        pricing=pricing,
    )
    adapter = _build_adapter(
        data_db_path=data_db,
        runtime_db_path=runtime_write_db_path,
        prepared_identity_ref=summary.get("data_runtime_identity_ref"),
        prepared_identity_hash=summary.get("data_runtime_identity_object_hash"),
        case_timeout_seconds=summary.get("case_timeout_seconds"),
        provider_budget=provider_budget,
        runner_adapter_factory=runner_adapter_factory,
    )

    attempts_by_case: dict[str, int] = {}
    for row in rows:
        if row.eval_id == eval_id:
            attempts_by_case[row.case_id] = max(
                attempts_by_case.get(row.case_id, 0), row.attempts
            )

    for case_id in ordered_ids:
        if case_id in completed:
            continue  # identity-valid COMPLETED row resumes; never rerun
        remaining_calls = max_provider_calls - used_calls
        remaining_cost = max_cost_usd - used_cost
        # Ceilings are enforced BEFORE every dispatch, including binding the
        # remaining budget into the production adapter.
        if remaining_calls <= 0 or remaining_cost <= 0:
            raise Stage1CeilingExceeded(
                f"ceiling reached before dispatch of {case_id!r}: "
                f"provider_calls={used_calls}/{max_provider_calls} "
                f"cost_usd={used_cost:.4f}/{max_cost_usd}"
            )
        bind_budget = getattr(adapter, "set_remaining_budget", None)
        if callable(bind_budget):
            bind_budget(provider_calls=remaining_calls, cost_usd=remaining_cost)
        if provider_budget is not None:
            # The real enforcement point: the shared guard bounds every
            # Analyst/Writer provider attempt inside this case.
            provider_budget.begin_case(
                provider_calls=remaining_calls, cost_usd=remaining_cost
            )
        outcome = _invoke_adapter(adapter, gold_by_case[case_id])
        if outcome.case_id != case_id:
            raise Stage1OperatorError(
                f"adapter returned outcome for {outcome.case_id!r} while "
                f"executing {case_id!r}"
            )
        if outcome.terminal_status == "COMPLETED":
            try:
                _verify_artifact_hashes(outcome)
            except RunArtifactHashError as exc:
                raise Stage1OperatorError(str(exc)) from exc
        row = _ledger_row_from_outcome(
            outcome,
            eval_id=eval_id,
            attempts=attempts_by_case.get(case_id, 0) + 1,
        )
        rows.append(row)
        # Every terminal case is atomically durable before any reconciliation
        # or fail-closed stop. An unknown cost is preserved as unknown, never
        # coerced to zero.
        ExecutionLedger(rows=tuple(rows)).write(ledger_path)
        if outcome.cost_usd is None:
            raise Stage1OperatorError(
                f"case {case_id!r} reported unknown provider cost; refusing to "
                "treat it as $0"
            )
        _reconcile_case_accounting(provider_budget, outcome)
        used_calls += outcome.provider_calls
        used_cost += outcome.cost_usd
        if outcome.terminal_status != "COMPLETED":
            # A failed/cancelled run carries no result artifacts to hash, but
            # the provider attempts it dispatched are real consumption. The
            # durable per-case accounting is reconciled against the shared
            # guard, the row records the real calls/cost, and the budget the
            # run consumed is deducted so a resume can never re-spend it.
            raise Stage1OperatorError(
                f"case {case_id!r} ended {outcome.terminal_status!r}; "
                "resumable evidence preserved, further dispatch stopped"
            )
        if used_calls > max_provider_calls or used_cost > max_cost_usd:
            ExecutionLedger(rows=tuple(rows)).write(ledger_path)
            raise Stage1CeilingExceeded(
                f"ceiling exceeded after {case_id!r}: "
                f"provider_calls={used_calls}/{max_provider_calls} "
                f"cost_usd={used_cost:.4f}/{max_cost_usd}; resumable "
                "evidence preserved"
            )

    success_ids = {
        row.case_id
        for row in rows
        if row.eval_id == eval_id
        and row.terminal_status == "COMPLETED"
        and row.identity_valid
    }
    if success_ids != set(ordered_ids):
        raise Stage1OperatorError(
            "execute did not cover all ordered cases with identity-valid "
            f"COMPLETED success rows (missing={sorted(set(ordered_ids) - success_ids)})"
        )
    _assert_data_db_unchanged(summary)
    ExecutionLedger(rows=tuple(rows)).write(ledger_path)
    print(f"execute OK: {len(ordered_ids)} ordered cases terminal success")
    return EXIT_OK


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

def _audit(*, output_dir: Path, audit: Path | None, **__) -> int:
    if audit is None or not audit.is_file():
        raise Stage1OperatorError(f"audit file not found: {audit}")
    summary = _load_prepare_summary(output_dir)
    eval_manifest, cases = _load_eval_manifest_and_cases(summary, output_dir)
    ledger_path = output_dir / LEDGER_FILENAME
    ledger = ExecutionLedger.load(
        ledger_path, expected_eval_id=eval_manifest.evaluation_identity.eval_id
    )
    audits = load_output_audit(audit)
    if not audits:
        raise Stage1OperatorError("audit file contains no rows")
    validate_audit_against_ledger(
        audits,
        eval_manifest=eval_manifest,
        gold_cases=cases,
        ledger=ledger,
    )
    print(f"audit OK: {len(audits)} sealed case audits validated against the ledger")
    return EXIT_OK


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _report(
    *,
    output_dir: Path,
    audit: Path | None,
    manifest_out: Path | None,
    json_out: Path | None,
    markdown_out: Path | None,
    **__,
) -> int:
    if audit is None or not audit.is_file():
        raise Stage1OperatorError(
            f"human output audit missing: {audit}; missing authority is a "
            "legitimate operator stop"
        )
    if manifest_out is None or json_out is None or markdown_out is None:
        raise Stage1OperatorError(
            "report requires --manifest-out, --json-out, and --markdown-out"
        )
    summary = _load_prepare_summary(output_dir)
    eval_manifest, cases = _load_eval_manifest_and_cases(summary, output_dir)
    eval_id = eval_manifest.evaluation_identity.eval_id
    ledger_path = output_dir / LEDGER_FILENAME
    ledger = ExecutionLedger.load(ledger_path, expected_eval_id=eval_id)
    audits = load_output_audit(audit)
    validate_audit_against_ledger(
        audits,
        eval_manifest=eval_manifest,
        gold_cases=cases,
        ledger=ledger,
    )
    recorded_strat_path = summary.get("stratification_path")
    if recorded_strat_path:
        strat_payload = json.loads(
            Path(recorded_strat_path).read_text(encoding="utf-8")
        )
    else:
        dataset_manifest = _load_dataset_manifest(
            Path(summary["dataset_manifest_path"])
        )
        strat_payload = dataset_manifest.get("stratification")
        if not isinstance(strat_payload, Mapping):
            raise Stage1OperatorError(
                "prepare summary records no stratification path and the dataset "
                "manifest has no stratification block"
            )
    payload = build_report_payload(
        eval_manifest=eval_manifest,
        gold_cases=cases,
        stratification=strat_payload,
        ledger=ledger,
        audits=audits,
        execution_head_sha8=summary["execution_head_sha8"],
        max_provider_calls=summary.get("max_provider_calls"),
        max_cost_usd=summary.get("max_cost_usd"),
    )
    findings = scan_report_for_secrets(payload)
    if findings:
        raise Stage1GateFailure(
            f"report contains secret-shaped text: {findings[:5]}"
        )
    write_report_json(json_out, payload)
    write_report_json(manifest_out, eval_manifest.model_dump(mode="json"))
    write_report_markdown(markdown_out, render_report_markdown(payload))
    print(f"report sealed: {json_out}")
    if payload.get("gates_passed") is not True:
        raise Stage1GateFailure(
            "one or more hard gates failed; the report was published as an "
            "explicit failed report, never as a passing seal"
        )
    return EXIT_OK


# ---------------------------------------------------------------------------
# all
# ---------------------------------------------------------------------------

def _all(
    *,
    dataset_manifest: Path | None,
    stratification: Path | None,
    output_dir: Path,
    max_provider_calls: int | None,
    max_cost_usd: float | None,
    audit: Path | None,
    manifest_out: Path | None,
    json_out: Path | None,
    markdown_out: Path | None,
    runtime_db: Path | None,
    runtime_identity_ref: str | None,
    runtime_identity_hash: str | None,
    q001_file_sha256: str | None,
    pricing: Path | None,
    runner_adapter_factory: Callable[[], Any] | None,
    **__,
) -> int:
    code = _prepare(
        dataset_manifest=dataset_manifest,
        stratification=stratification,
        output_dir=output_dir,
        max_provider_calls=max_provider_calls,
        max_cost_usd=max_cost_usd,
        runtime_db=runtime_db,
        runtime_identity_ref=runtime_identity_ref,
        runtime_identity_hash=runtime_identity_hash,
        q001_file_sha256=q001_file_sha256,
    )
    if code != EXIT_OK:
        return code
    code = _execute(
        output_dir=output_dir,
        max_provider_calls=max_provider_calls,
        max_cost_usd=max_cost_usd,
        pricing=pricing,
        runner_adapter_factory=runner_adapter_factory,
    )
    if code != EXIT_OK:
        return code
    code = _audit(output_dir=output_dir, audit=audit)
    if code != EXIT_OK:
        return code
    return _report(
        output_dir=output_dir,
        audit=audit,
        manifest_out=manifest_out,
        json_out=json_out,
        markdown_out=markdown_out,
    )


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "execute", "audit", "report", "all"))
    parser.add_argument(
        "--dataset-manifest", type=Path,
        help=(
            "Benchmark dataset manifest (cases.jsonl plus manifest metadata); "
            "execution ceilings are not accepted in this file."
        ),
    )
    parser.add_argument(
        "--stratification", type=Path,
        help=(
            "Benchmark stratification JSON used to bind per-case coverage "
            "and challenge-family metadata."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/eval_reports/v1_1_stage1"),
        help=(
            "Output directory for the ledger, prepare summary, and the "
            "writable M6 runtime database (runtime.sqlite3)."
        ),
    )
    parser.add_argument(
        "--max-provider-calls",
        type=int,
        help=(
            "Approved total provider-call ceiling. prepare records it; "
            "execute must pass the exact same value."
        ),
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        help=(
            "Approved total USD cost ceiling. prepare records it; execute must "
            "pass the exact same value."
        ),
    )
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--manifest-out", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument(
        "--runtime-db",
        type=Path,
        help=(
            "Required for prepare/all: the immutable Q-001 operational "
            "derivative SQLite database. It is hashed incrementally, is never "
            "initialized/WAL-enabled/migrated, and is never the writable M6 "
            "runtime DB (that is always <output-dir>/runtime.sqlite3)."
        ),
    )
    parser.add_argument(
        "--pricing-json",
        type=Path,
        help=(
            "Operator-supplied identity-bound model prices and per-role token "
            "limits (non-secret). Required for a positive --max-cost-usd; "
            "without it the provider budget guard fails closed before "
            "dispatch. Format: {\"models\": {\"<provider>/<model_id>\": "
            "{\"input_usd_per_million_tokens\": .., "
            "\"output_usd_per_million_tokens\": ..}}, \"role_token_limits\": "
            "{\"evidence_analyst\": {\"max_input_tokens\": .., "
            "\"max_output_tokens\": ..}, \"streaming_writer\": {..}}}. Role "
            "keys are the agents' role constants."
        ),
    )
    parser.add_argument(
        "--runtime-identity-ref",
        default=None,
        help=(
            "DataRuntimeIdentity object reference; falls back to the dataset "
            "manifest data_runtime_identity_ref."
        ),
    )
    parser.add_argument(
        "--runtime-identity-hash",
        default=None,
        help=(
            "DataRuntimeIdentity object SHA-256; this is distinct from the "
            "Q-001 file SHA. Falls back to the dataset manifest "
            "data_runtime_identity_hash."
        ),
    )
    parser.add_argument(
        "--q001-file-sha256",
        default=None,
        help=(
            "SHA-256 of the immutable Q-001 runtime DB file. This is a "
            "separate binding from --runtime-identity-hash and falls back to "
            "q001_file_sha256 in the dataset manifest."
        ),
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner_adapter_factory: Callable[[], Any] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            return _prepare(
                dataset_manifest=args.dataset_manifest,
                stratification=args.stratification,
                output_dir=args.output_dir,
                max_provider_calls=args.max_provider_calls,
                max_cost_usd=args.max_cost_usd,
                runtime_db=args.runtime_db,
                runtime_identity_ref=args.runtime_identity_ref,
                runtime_identity_hash=args.runtime_identity_hash,
                q001_file_sha256=args.q001_file_sha256,
            )
        if args.command == "execute":
            return _execute(
                output_dir=args.output_dir,
                max_provider_calls=args.max_provider_calls,
                max_cost_usd=args.max_cost_usd,
                pricing=args.pricing_json,
                runner_adapter_factory=runner_adapter_factory,
            )
        if args.command == "audit":
            return _audit(output_dir=args.output_dir, audit=args.audit)
        if args.command == "report":
            return _report(
                output_dir=args.output_dir,
                audit=args.audit,
                manifest_out=args.manifest_out,
                json_out=args.json_out,
                markdown_out=args.markdown_out,
            )
        if args.command == "all":
            return _all(
                dataset_manifest=args.dataset_manifest,
                stratification=args.stratification,
                output_dir=args.output_dir,
                max_provider_calls=args.max_provider_calls,
                max_cost_usd=args.max_cost_usd,
                audit=args.audit,
                manifest_out=args.manifest_out,
                json_out=args.json_out,
                markdown_out=args.markdown_out,
                runtime_db=args.runtime_db,
                runtime_identity_ref=args.runtime_identity_ref,
                runtime_identity_hash=args.runtime_identity_hash,
                q001_file_sha256=args.q001_file_sha256,
                pricing=args.pricing_json,
                runner_adapter_factory=runner_adapter_factory,
            )
        raise Stage1OperatorError(f"unknown command {args.command!r}")
    except (Stage1OperatorError, Stage1CeilingExceeded) as exc:
        print(f"operator/contract error: {exc}", file=sys.stderr)
        return EXIT_CONTRACT_ERROR
    except Stage1GateFailure as exc:
        print(f"gate failure: {exc}", file=sys.stderr)
        return EXIT_GATE_FAILURE
    except Exception as exc:  # pragma: no cover - defensive
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONTRACT_ERROR


if __name__ == "__main__":
    sys.exit(main())
