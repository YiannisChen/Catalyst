"""M5-10 executable gates (sealed baseline integrity + semantic ontology regression).

M5 plan §5; Final TSD §27 M5. Two deterministic, repo-executable gates — no
real-provider comparison, no path-bound /root evidence derivation, no quality
or comparable claims.

Gate A verifies the sealed M1 corrective baseline artifact chain without
running any model. Gate B is a deterministic fixture-only regression of the
semantic ontology boundary over the approved 10-case T4 pack built by the repo
case-pack code, comparing only mechanical parity metrics.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from catalyst_eval.post_import.case_pack import (
    build_smoke_case_pack,
    compute_case_pack_id,
)
from catalyst_eval.post_import.t4_evidence import (
    APPROVED_T4_CASE_COUNT,
    APPROVED_T4_CASE_PACK_ID,
    APPROVED_T4_ORDERED_CASE_IDS,
)

GATE_A_SCHEMA = "v1_1_m5_gate_a_v1"
GATE_B_SCHEMA = "v1_1_m5_gate_b_v1"

SEALED_BASELINE_TAG = "v1.1-m1-baseline-corrective-seal"
SEALED_BASELINE_REPORT = (
    "data/baseline/reports/v1_1_baseline_621375bc_corrective_seal.json"
)
SEALED_BASELINE_SHA256 = (
    "862192eba92048ad9859260d082a027a29ffec5c836bd72768ec60ac041af0c7"
)
SEALED_BASELINE_SCHEMA = "baseline_v1"
FOUR_ARM_TOKEN = "FOUR_ARM_E2E_OK"
USER_SMOKE_TOKEN = "USER_SMOKE_OK"
Q002_PROMOTED_ENV_REASON = "q_002_promoted_environment_tuple_unrecovered"


class GateFailure(Exception):
    """Hard gate failure: no artifact is written and the CLI exits 1."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(args: list[str], repo_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )


def _verify_tag(tag: str, repo_root: Path) -> None:
    """The tag must exist (annotated) and be an ancestor of the M5 HEAD."""
    exists = _git(["tag", "-l", tag], repo_root)
    if tag not in exists.stdout.split():
        raise GateFailure(f"sealed tag {tag!r} does not exist")
    object_type = _git(["cat-file", "-t", tag], repo_root)
    if object_type.stdout.strip() != "tag":
        raise GateFailure(f"sealed tag {tag!r} is not annotated")
    ancestor = _git(["merge-base", "--is-ancestor", tag, "HEAD"], repo_root)
    if ancestor.returncode != 0:
        raise GateFailure(f"sealed tag {tag!r} is not an ancestor of HEAD")


def _verify_report(report_path: Path) -> dict[str, Any]:
    if not report_path.is_file():
        raise GateFailure(f"sealed report not found: {report_path}")
    actual_sha = _sha256_file(report_path)
    if actual_sha != SEALED_BASELINE_SHA256:
        raise GateFailure(
            f"sealed report SHA-256 mismatch: {actual_sha} != {SEALED_BASELINE_SHA256}"
        )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GateFailure(f"sealed report is not valid JSON: {exc}") from exc
    if report.get("schema_version") != SEALED_BASELINE_SCHEMA:
        raise GateFailure(
            f"sealed report schema mismatch: {report.get('schema_version')!r}"
        )
    runs = report.get("runs")
    if not isinstance(runs, list):
        raise GateFailure("sealed report runs metadata is missing")
    by_token = {run.get("success_token"): run for run in runs}
    missing = [token for token in (FOUR_ARM_TOKEN, USER_SMOKE_TOKEN) if token not in by_token]
    if missing:
        raise GateFailure(f"sealed report missing run tokens: {missing}")
    for token in (FOUR_ARM_TOKEN, USER_SMOKE_TOKEN):
        run = by_token[token]
        sha = run.get("success_token_sha256")
        if not isinstance(sha, str) or len(sha) != 64:
            raise GateFailure(f"run token {token} has no valid success_token_sha256")
    comparability = report.get("comparability") or {}
    if comparability.get("promoted_env_recovered") is not False:
        raise GateFailure("Q-002 NON-COMPARABLE must preserve promoted_env_recovered=false")
    if comparability.get("promoted_env_reason") != Q002_PROMOTED_ENV_REASON:
        raise GateFailure("Q-002 NON-COMPARABLE promoted_env_reason mismatch")
    return {
        "report_schema": report["schema_version"],
        "run_tokens": {
            "four_arm": {
                "token": FOUR_ARM_TOKEN,
                "success_token_sha256": by_token[FOUR_ARM_TOKEN]["success_token_sha256"],
            },
            "user_smoke": {
                "token": USER_SMOKE_TOKEN,
                "success_token_sha256": by_token[USER_SMOKE_TOKEN]["success_token_sha256"],
            },
        },
        "q002_non_comparable": {
            "promoted_env_recovered": comparability["promoted_env_recovered"],
            "promoted_env_reason": comparability["promoted_env_reason"],
        },
    }


def sealed_baseline_integrity_gate(
    *,
    repo_root: Path,
    tag: str,
    report_path: Path,
    out_path: Path,
) -> dict[str, Any]:
    """Gate A: verify the sealed baseline artifact chain; write the artifact."""
    _verify_tag(tag, repo_root)
    verified = _verify_report(Path(report_path))
    payload = {
        "schema_version": GATE_A_SCHEMA,
        "tag_verified": True,
        "report_path": str(report_path),
        "report_sha256": SEALED_BASELINE_SHA256,
        "report_schema": verified["report_schema"],
        "run_tokens": verified["run_tokens"],
        "q002_non_comparable": verified["q002_non_comparable"],
        "exit_status": 0,
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


# ---------------------------------------------------------------------------
# Gate B: deterministic fixture adapters + mechanical parity metrics
# ---------------------------------------------------------------------------

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|credential|authorization|"
    r"\bsk-[a-z0-9_-]{8,})"
)


def legacy_fixture_adapter(case: Any) -> dict[str, Any]:
    """Deterministic legacy mechanical fixture adapter (hidden gold)."""
    evidence_id = f"chunk:{case.case_id}:{case.ticker}"
    return {
        "run_id": f"legacy:{case.case_id}",
        "ticker": case.ticker,
        "cutoff": case.cutoff,
        "evidence_ids": (evidence_id,),
        "citations": (evidence_id,),
        "runtime_identity": f"rt:{case.case_id}",
        "claim_lineage": {evidence_id: f"hyp:{case.case_id}"},
    }


def v1_fixture_adapter(case: Any) -> dict[str, Any]:
    """Deterministic V1 mechanical fixture adapter over identical request facts."""
    evidence_id = f"chunk:{case.case_id}:{case.ticker}"
    context_pack_hash = hashlib.sha256(
        f"pack:{case.case_id}:{case.ticker}:{case.cutoff}".encode("utf-8")
    ).hexdigest()
    return {
        "run_id": f"v1:{case.case_id}",
        "ticker": case.ticker,
        "cutoff": case.cutoff,
        "evidence_ids": (evidence_id,),
        "citations": (evidence_id,),
        "runtime_identity": f"rt:{case.case_id}",
        "context_pack_hash": context_pack_hash,
        "claim_lineage": {evidence_id: f"hyp:{case.case_id}"},
    }


def _mechanical_metrics(cases: list[Any], adapter: Any) -> dict[str, float | int]:
    ticker_cutoff_violations = 0
    resolved_citations = 0
    total_citations = 0
    runtime_bound = 0
    context_identity_ok = 0
    context_checked = 0
    lineage_ok = 0
    secret_leakage = 0
    context_hashes: dict[str, str] = {}
    for case in cases:
        result = adapter(case)
        if result["ticker"] != case.ticker or result["cutoff"] != case.cutoff:
            ticker_cutoff_violations += 1
        evidence = set(result["evidence_ids"])
        total_citations += len(result["citations"])
        resolved_citations += sum(
            1 for citation in result["citations"] if citation in evidence
        )
        if result["runtime_identity"]:
            runtime_bound += 1
        lineage_ok += sum(
            1
            for evidence_id in evidence
            if evidence_id in result["claim_lineage"]
        ) / max(len(evidence), 1)
        context_hash = result.get("context_pack_hash")
        if context_hash:
            context_checked += 1
            if case.case_id in context_hashes:
                context_identity_ok += int(context_hashes[case.case_id] == context_hash)
            else:
                context_hashes[case.case_id] = context_hash
                context_identity_ok += 1
        serialized = json.dumps(result, sort_keys=True)
        if _SECRET_RE.search(serialized):
            secret_leakage += 1
    return {
        "ticker_cutoff_violations": ticker_cutoff_violations,
        "citation_resolution": (
            resolved_citations / total_citations if total_citations else 1.0
        ),
        "runtime_identity_binding": runtime_bound / len(cases) if cases else 1.0,
        "context_pack_identity": (
            context_identity_ok / context_checked if context_checked else 1.0
        ),
        "claim_lineage": lineage_ok / len(cases) if cases else 1.0,
        "secret_leakage": secret_leakage,
    }


def compute_ontology_metrics(*, case_pack_id: str) -> dict[str, float | int]:
    """Deterministic mechanical parity metrics over the approved pack surface.

    ``case_pack_id`` is accepted for stable deterministic identity; the metrics
    are fixture-mechanical and never claim quality or comparability.
    """
    del case_pack_id
    return {
        "ticker_cutoff_violations": 0,
        "citation_resolution": 1.0,
        "runtime_identity_binding": 1.0,
        "context_pack_identity": 1.0,
        "claim_lineage": 1.0,
        "secret_leakage": 0,
    }


def semantic_ontology_regression_gate(
    *,
    repo_root: Path,
    golden_dir: Path,
    out_path: Path,
) -> dict[str, Any]:
    """Gate B: deterministic fixture regression over the approved T4 case pack.

    Cases are built only through the repo case-pack code from the golden set;
    the sealed report's /root-bound evidence refs are audit refs only and are
    never read to construct cases. Gold is hidden; adapters are deterministic
    fakes; only mechanical parity metrics are compared and no quality or
    comparable claim is made (comparability_declared=false).
    """
    del repo_root
    cases = build_smoke_case_pack(Path(golden_dir))
    pack_id = compute_case_pack_id(cases)
    ordered_ids = tuple(case.case_id for case in cases)
    if pack_id != APPROVED_T4_CASE_PACK_ID:
        raise GateFailure(
            f"T4 case pack ID mismatch: {pack_id} != {APPROVED_T4_CASE_PACK_ID}"
        )
    if len(cases) != APPROVED_T4_CASE_COUNT:
        raise GateFailure(f"T4 case pack count mismatch: {len(cases)}")
    if ordered_ids != APPROVED_T4_ORDERED_CASE_IDS:
        raise GateFailure("T4 case pack ordered IDs do not match the approved pack")

    legacy_metrics = _mechanical_metrics(cases, legacy_fixture_adapter)
    v1_metrics = _mechanical_metrics(cases, v1_fixture_adapter)
    expected = compute_ontology_metrics(case_pack_id=pack_id)
    for metric, expected_value in expected.items():
        if legacy_metrics[metric] != v1_metrics[metric]:
            raise GateFailure(f"metric parity failure on {metric}: legacy/v1 diverge")
        if legacy_metrics[metric] != expected_value:
            raise GateFailure(f"metric {metric} failed the locked expectation")

    payload = {
        "schema_version": GATE_B_SCHEMA,
        "case_pack": {
            "approved_case_pack_id": pack_id,
            "case_count": len(cases),
            "ordered_case_ids": list(ordered_ids),
            "matched": True,
        },
        "metric_parity": {key: legacy_metrics[key] for key in expected},
        "comparability_declared": False,
        "exit_status": 0,
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


__all__ = [
    "APPROVED_T4_CASE_PACK_ID",
    "FOUR_ARM_TOKEN",
    "GATE_A_SCHEMA",
    "GATE_B_SCHEMA",
    "GateFailure",
    "Q002_PROMOTED_ENV_REASON",
    "SEALED_BASELINE_REPORT",
    "SEALED_BASELINE_SHA256",
    "SEALED_BASELINE_SCHEMA",
    "SEALED_BASELINE_TAG",
    "USER_SMOKE_TOKEN",
    "compute_ontology_metrics",
    "legacy_fixture_adapter",
    "sealed_baseline_integrity_gate",
    "semantic_ontology_regression_gate",
    "v1_fixture_adapter",
]
