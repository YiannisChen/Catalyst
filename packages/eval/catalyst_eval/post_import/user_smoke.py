"""Wave 3 production attribution user-smoke runner (importable core).

The smoke exercises the real user-visible path:

    loopback HTTP -> Catalyst app -> live run -> runtime dependency loader
    -> attribution graph -> retrieval/tool execution -> DeepSeek answer
    -> exported trace / RunAssuranceRecord / immutable evidence

Production entry point: ``packages/eval/scripts/run_post_import_user_smoke.py``.
This module contains the testable orchestration and fail-closed validation
shared by that CLI.  Direct graph-internal invocation is allowed only in unit
tests, never as the production smoke execution path.

Gates (fail closed before provider use):

- exact clean Git runtime identity resolved from disk;
- frozen DB identity/counts and LanceDB/index manifest identities validated;
- T4 evidence validates against the selected case pack/runtime identity;
- a successful full Wave 2 evidence directory exists with a code-produced
  ``FOUR_ARM_E2E_OK`` (never CLI-supplied);
- CUDA + production pinned embedding/reranker boundaries satisfied;
- app dependencies are wired with the explicit authoritative index manifest
  path;
- provider/model validation succeeds without exposing credentials.

The CLI never accepts a caller-supplied success token, fake validation
boolean, raw identity strings, or an API key argument.  ``USER_SMOKE_OK`` is
written only when every gate, the three expected statuses, trace/assurance
checks, evidence identity checks, cost gate, failure-path checks, and secret
scan pass; it is never written for partial/mocked/degraded/unknown-cost runs,
for ``--limit``-style partial execution, or on expectation mismatch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from catalyst_data.corpus.streaming_publication import served_chunks_relation
from catalyst_data.retrieval.result import SEARCHABLE_STATUSES
from catalyst_agents.runtime.assurance.record import RunAssuranceRecord
from catalyst_agents.trace.exporter import export_run

from catalyst_eval.post_import.case_pack import CasePackCase
from catalyst_eval.post_import.four_arm import (
    EmbeddingBoundary,
    RunnerValidationError,
    validate_embedding_boundary,
)
from catalyst_eval.post_import.index_identity import ResolvedRuntimeIdentity
from catalyst_eval.post_import.t4_evidence import (
    ValidatedT4Evidence,
    validate_t4_evidence,
)

USER_SMOKE_TOKEN = "USER_SMOKE_OK"
WAVE_TOKEN_FILENAME = "WAVE_TOKEN.txt"
WAVE2_FOUR_ARM_TOKEN = "FOUR_ARM_E2E_OK"
META_SCHEMA_VERSION = "post_import_user_smoke_meta_v1"
CASE_RESULTS_SCHEMA = "post_import_user_smoke_case_results_v1"
FAILURE_PATHS_SCHEMA = "post_import_user_smoke_failure_paths_v1"

# Stable approved Wave 3 selection (from the approved golden/case-pack sources).
USER_SMOKE_CASE_IDS = ("g006", "g007", "h004")
USER_SMOKE_EXPECTED_CLASSES = {
    "g006": "SUFFICIENT",
    "g007": "PARTIAL",
    "h004": "ABSTAIN",
}

COST_CEILING_USD = 5.0
POLL_DEFAULT_TIMEOUT_SECONDS = 300.0
POLL_DEFAULT_INTERVAL_SECONDS = 0.5
DEFAULT_PROVIDER = "deepseek"
DEFAULT_MODEL_ID = "deepseek-chat"

EXPECTED_WAVE2_CASE_COUNT = 10

_TERMINAL_STATUSES = {
    "SUCCEEDED", "SUFFICIENT", "PARTIAL", "ABSTAIN",
    "FAILED_SYSTEM", "SYSTEM_ERROR", "FAILED_REQUEST", "CANCELLED",
}
_FAILED_TRANSPORT = {"FAILED_SYSTEM", "SYSTEM_ERROR", "FAILED_REQUEST", "CANCELLED"}

_SECRET_KEYS = frozenset({
    "authorization", "api_key", "x-api-key", "cookie", "set-cookie",
    "proxy-authorization", "deepseek_api_key", "openai_api_key",
    "anthropic_api_key", "aihubmix_api_key", "siliconflow_api_key",
    "gemini_api_key", "glm_api_key",
})
_SECRET_VALUE_RE = re.compile(
    r"(?i)(sk-[a-z0-9_-]{8,}|[a-z0-9_-]+_api_key\s*[:=]\s*\S+)"
)
_SECRET_SCAN_PATTERNS = (
    re.compile(r"(?i)sk-[a-z0-9_-]{8,}"),
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(deepseek|openai|anthropic|aihubmix|siliconflow|gemini|glm|zai)_api_key\s*[:=]\s*\S+"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    _atomic_write_text(path, text)


def _open_frozen_db_readonly(path: Path) -> sqlite3.Connection:
    """Open the protected frozen DB with an immutable read-only URI.

    The runner never opens the source protected DB writable; all runtime
    writes go to the writable derivative DB provided by the operator.
    """
    uri = f"{Path(path).resolve().as_uri()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


# ---------------------------------------------------------------------------
# Stable case selection
# ---------------------------------------------------------------------------


def normalize_expected_class(raw_status: str) -> str:
    """Normalize the golden status at the documented user-smoke boundary."""
    if raw_status == "INSUFFICIENT":
        return "ABSTAIN"
    return raw_status


@dataclass(frozen=True)
class UserSmokeCase:
    case: CasePackCase
    expected_class: str
    raw_expected_status: str


def select_user_smoke_cases(cases: list[CasePackCase]) -> list[UserSmokeCase]:
    """Select exactly the three approved cases by stable ID.

    Preserves the h004 query_override from the approved source; never hand
    copies or silently mutates golden evidence fields.
    """
    by_id: dict[str, CasePackCase] = {}
    for case in cases:
        if case.case_id in by_id:
            raise ValueError(f"duplicate case_id in case pack: {case.case_id}")
        by_id[case.case_id] = case
    missing = [case_id for case_id in USER_SMOKE_CASE_IDS if case_id not in by_id]
    if missing:
        raise ValueError(f"user-smoke cases missing from case pack: {', '.join(missing)}")
    selected: list[UserSmokeCase] = []
    for case_id in USER_SMOKE_CASE_IDS:
        case = by_id[case_id]
        raw = str(case.golden.get("expected_status") or "")
        expected_class = USER_SMOKE_EXPECTED_CLASSES[case_id]
        if raw and normalize_expected_class(raw) != expected_class:
            raise ValueError(
                f"golden expected_status {raw!r} for {case_id} does not match approved class {expected_class}"
            )
        selected.append(UserSmokeCase(
            case=case,
            expected_class=expected_class,
            raw_expected_status=raw,
        ))
    return selected


# ---------------------------------------------------------------------------
# Wave 2 evidence validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidatedWave2Evidence:
    wave2_dir: Path
    four_arm_token: str
    four_arm_meta: dict[str, Any]
    t4_evidence: ValidatedT4Evidence


def validate_wave2_evidence(
    *,
    wave2_dir: Path,
    t4_evidence_dir: Path,
    current_case_pack: list[CasePackCase] | None,
    resolved: ResolvedRuntimeIdentity | None,
) -> ValidatedWave2Evidence:
    """Fail-closed validation of the full Wave 2 evidence directory.

    The success token must exist with the exact code-produced content
    ``FOUR_ARM_E2E_OK``; it is never supplied by CLI or user.  The four-arm
    meta must be identity-bound to the resolved runtime and describe a full
    (non-limited) production-pinned 10-case run.
    """
    wave2_dir = Path(wave2_dir).resolve()
    if not wave2_dir.is_dir():
        raise ValueError(f"wave2 evidence directory missing: {wave2_dir}")

    token_path = wave2_dir / WAVE_TOKEN_FILENAME
    if not token_path.is_file():
        raise ValueError(f"{WAVE_TOKEN_FILENAME} missing in wave2 evidence directory")
    token = token_path.read_text(encoding="utf-8").strip()
    if token != WAVE2_FOUR_ARM_TOKEN:
        raise ValueError(f"{WAVE_TOKEN_FILENAME} content must be {WAVE2_FOUR_ARM_TOKEN}")

    meta_path = wave2_dir / "meta.json"
    if not meta_path.is_file():
        raise ValueError("wave2 meta.json missing")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise ValueError("wave2 meta.json must be an object")

    if meta.get("embedding_mode") != "production_pinned":
        raise ValueError("wave2 run must be production_pinned")
    case_count = meta.get("case_count")
    full_case_count = meta.get("full_case_count")
    if case_count != EXPECTED_WAVE2_CASE_COUNT or full_case_count != EXPECTED_WAVE2_CASE_COUNT:
        raise ValueError(
            f"wave2 run must be a full {EXPECTED_WAVE2_CASE_COUNT}-case run"
        )

    if resolved is not None:
        comparisons = (
            ("runtime_git_head", meta.get("runtime_git_head"), resolved.git_head),
            ("db_sha256", meta.get("db_sha256"), resolved.db_sha256),
            ("index_manifest_id", meta.get("index_manifest_id"), resolved.index_manifest_id),
            ("snapshot_id", meta.get("snapshot_id"), resolved.snapshot_id),
            ("corpus_manifest_id", meta.get("corpus_manifest_id"), resolved.corpus_manifest_id),
        )
        for label, actual, expected in comparisons:
            if actual != expected:
                raise ValueError(f"wave2 meta.{label} does not match resolved runtime identity")

    arms_dir = wave2_dir / "arms"
    pool_dir = wave2_dir / "pool"
    if not arms_dir.is_dir() or not pool_dir.is_dir():
        raise ValueError("wave2 arms/ or pool/ directory missing")
    arm_files = sorted(arms_dir.glob("*.json"))
    pool_files = sorted(pool_dir.glob("*.json"))
    if len(arm_files) != EXPECTED_WAVE2_CASE_COUNT:
        raise ValueError(
            f"wave2 arms must contain {EXPECTED_WAVE2_CASE_COUNT} artifacts"
        )
    if len(pool_files) != EXPECTED_WAVE2_CASE_COUNT:
        raise ValueError(
            f"wave2 pool must contain {EXPECTED_WAVE2_CASE_COUNT} artifacts"
        )

    t4_evidence = validate_t4_evidence(
        evidence_dir=t4_evidence_dir,
        current_case_pack=current_case_pack,
        resolved=resolved,
    )
    return ValidatedWave2Evidence(
        wave2_dir=wave2_dir,
        four_arm_token=token,
        four_arm_meta=meta,
        t4_evidence=t4_evidence,
    )


# ---------------------------------------------------------------------------
# Evidence/citation identity
# ---------------------------------------------------------------------------


def _chunk_served_for_case(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    manifest_id: str,
    ticker: str,
    cutoff: str,
) -> tuple[str | None, str | None]:
    """Return (available_at, source_class) when the chunk is served for the case."""
    relation = served_chunks_relation(conn)
    status_placeholders = ", ".join("?" for _ in SEARCHABLE_STATUSES)
    row = conn.execute(
        f"""SELECT c.available_at, c.source_class
            FROM {relation} c
            WHERE c.chunk_id = ?
              AND c.manifest_id = ?
              AND c.status IN ({status_placeholders})
              AND c.eligibility = 'eligible'
              AND c.available_at <= ?
              AND EXISTS (
                SELECT 1 FROM json_each(c.ticker_associations) je
                WHERE je.value = ?
              )""",
        (chunk_id, manifest_id, *SEARCHABLE_STATUSES, cutoff, ticker),
    ).fetchone()
    if row is None:
        return None, None
    return str(row[0]), str(row[1])


def _validate_citations(
    frozen_db_path: Path,
    *,
    case: CasePackCase,
    cited_ids: list[str],
    manifest_id: str,
) -> tuple[bool, str]:
    """Every citation must resolve to a served frozen-corpus chunk within cutoff."""
    if not cited_ids:
        return True, "no citations to validate"
    unique = sorted(set(cited_ids))
    conn = _open_frozen_db_readonly(frozen_db_path)
    try:
        unresolvable: list[str] = []
        lookahead: list[str] = []
        for chunk_id in unique:
            available_at, _ = _chunk_served_for_case(
                conn,
                chunk_id=chunk_id,
                manifest_id=manifest_id,
                ticker=case.ticker,
                cutoff=case.cutoff,
            )
            if available_at is None:
                unresolvable.append(chunk_id)
            elif available_at > case.cutoff:
                lookahead.append(chunk_id)
    finally:
        conn.close()
    problems: list[str] = []
    if unresolvable:
        problems.append(f"unresolvable citations: {sorted(unresolvable)}")
    if lookahead:
        problems.append(f"look-ahead citations after cutoff: {sorted(lookahead)}")
    return (not problems), "; ".join(problems) if problems else "citations resolve"


def _cited_ids_from_workspace(workspace: dict[str, Any]) -> list[str]:
    result = workspace.get("result")
    if not isinstance(result, dict):
        return []
    cited: list[str] = []
    for cause in result.get("causes") or []:
        if not isinstance(cause, dict):
            continue
        cited.extend(cause.get("evidence_ids") or [])
    return cited


def _aggregate_cost(events: list[dict[str, Any]]) -> tuple[float | None, bool]:
    total = 0.0
    for event in events:
        cost = event.get("cost_usd")
        if cost is None:
            return None, False
        total += float(cost or 0.0)
    if not events:
        return None, False
    return total, True


def _provider_calls_in_events(events: list[dict[str, Any]]) -> int:
    return sum(1 for event in events if event.get("model_id"))


def _run_latency_ms(workspace: dict[str, Any], events: list[dict[str, Any]]) -> int | None:
    runtime_ms = workspace.get("runtime_ms")
    if isinstance(runtime_ms, int):
        return runtime_ms
    latencies = [event.get("latency_ms") for event in events if isinstance(event.get("latency_ms"), int)]
    return sum(latencies) if latencies else None


# ---------------------------------------------------------------------------
# Redaction and secret scan
# ---------------------------------------------------------------------------


def _redact_payload(payload: Any) -> Any:
    """Recursively redact secret-shaped keys and values (never echoes keys)."""
    if isinstance(payload, dict):
        return {
            key: "[REDACTED]" if str(key).lower() in _SECRET_KEYS else _redact_payload(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_redact_payload(item) for item in payload]
    if isinstance(payload, str):
        if _SECRET_VALUE_RE.search(payload):
            return "[REDACTED]"
        return payload
    return payload


def _secret_scan(directory: Path, *, secret_values: tuple[str, ...] = ()) -> list[str]:
    """Scan evidence files for configured secret values or secret-shaped text."""
    violations: list[str] = []
    for path in Path(directory).rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for value in secret_values:
            if value and value in text:
                violations.append(f"{path.name}: configured secret value present")
        for pattern in _SECRET_SCAN_PATTERNS:
            found = False
            for match in pattern.finditer(text):
                if "[REDACTED]" in match.group(0):
                    continue
                violations.append(f"{path.name}: secret pattern {pattern.pattern!r}")
                found = True
                break
            if found:
                break
    return violations


# ---------------------------------------------------------------------------
# HTTP session seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    json: Any = None
    headers: dict[str, str] = field(default_factory=dict)


def _require_json_response(resp: HttpResponse, *, endpoint: str) -> dict[str, Any]:
    if resp.status_code != 200:
        raise RunnerValidationError(f"{endpoint} returned HTTP {resp.status_code}")
    payload = resp.json
    if not isinstance(payload, dict):
        raise RunnerValidationError(f"{endpoint} returned a non-object payload")
    return payload


def _require_ready_health(http: Any) -> dict[str, Any]:
    payload = _require_json_response(http.get("/api/health/runtime"), endpoint="health")
    if payload.get("status") != "ready":
        raise RunnerValidationError(
            f"runtime health not ready: {payload.get('status')} "
            f"{payload.get('errors') or []}"
        )
    return payload


def _validate_provider_http(
    http: Any,
    *,
    provider: str,
    model_id: str,
    base_url: str | None,
    credential_source: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "provider": provider,
        "model_id": model_id,
        "api_key": "",
        "credential_source": credential_source,
    }
    if base_url is not None:
        body["base_url"] = base_url
    payload = _require_json_response(
        http.post("/api/models/validate", json=body),
        endpoint="provider validation",
    )
    if payload.get("ok") is not True:
        raise RunnerValidationError(
            f"provider/model validation failed: {payload.get('status')} {payload.get('message') or ''}"
        )
    return payload


def _model_config(
    *,
    provider: str,
    model_id: str,
    base_url: str | None,
    credential_source: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "provider": provider,
        "model_id": model_id,
        "credential_source": credential_source,
    }
    if base_url is not None:
        body["base_url"] = base_url
    return body


# ---------------------------------------------------------------------------
# Failure paths (same public boundary, provider calls asserted zero)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailurePathResult:
    name: str
    ok: bool
    detail: str
    provider_calls: int


def _submission_failed_request(resp: HttpResponse, *, expected_reasons: set[str]) -> tuple[bool, str]:
    payload = resp.json
    if resp.status_code != 200 or not isinstance(payload, dict):
        return False, f"unexpected HTTP {resp.status_code}"
    if payload.get("status") != "FAILED_REQUEST":
        return False, f"expected FAILED_REQUEST, got {payload.get('status')}"
    failure = payload.get("failure") or {}
    sub_reason = failure.get("sub_reason")
    if sub_reason not in expected_reasons:
        return False, f"expected sub_reason in {sorted(expected_reasons)}, got {sub_reason!r}"
    return True, sub_reason


def _exercise_failure_paths(
    http: Any,
    *,
    provider: str,
    model_id: str,
    base_url: str | None,
    credential_source: str,
) -> list[FailurePathResult]:
    """Deterministic no-provider failure paths through the public HTTP boundary."""
    results: list[FailurePathResult] = []
    model = _model_config(
        provider=provider,
        model_id=model_id,
        base_url=base_url,
        credential_source=credential_source,
    )

    # invalid ticker/input
    resp = http.post("/api/live-runs", json={
        "ticker": "ZZZZ",
        "trade_date": "2025-07-24",
        "query": "Why did ZZZZ move?",
        "model": model,
        "config": "mcj_full",
    })
    ok, detail = _submission_failed_request(resp, expected_reasons={"unsupported_ticker"})
    results.append(FailurePathResult("invalid_ticker", ok, detail, provider_calls=0))

    # invalid/future cutoff/date
    resp = http.post("/api/live-runs", json={
        "ticker": "TSLA",
        "trade_date": "2099-01-01",
        "query": "Why did TSLA move?",
        "model": model,
        "config": "mcj_full",
    })
    ok, detail = _submission_failed_request(
        resp,
        expected_reasons={"date_out_of_range", "missing_trading_day_context", "invalid_trade_date"},
    )
    results.append(FailurePathResult("invalid_trade_date", ok, detail, provider_calls=0))

    # provider credential absent (browser_key with no api_key -> zero network)
    resp = http.post("/api/models/validate", json={
        "provider": provider,
        "model_id": model_id,
        "api_key": "",
        "credential_source": "browser_key",
    })
    payload = resp.json
    ok = isinstance(payload, dict) and payload.get("status") == "auth_failed"
    results.append(FailurePathResult(
        "missing_provider_credential", ok, str(payload.get("message") or payload), provider_calls=0,
    ))

    # model validation failure (unknown provider -> invalid, zero network)
    resp = http.post("/api/models/validate", json={
        "provider": "unknown_provider",
        "model_id": model_id,
        "api_key": "placeholder-key",
        "credential_source": "browser_key",
    })
    payload = resp.json
    ok = isinstance(payload, dict) and payload.get("status") == "invalid"
    results.append(FailurePathResult(
        "model_validation_failure", ok, str(payload.get("message") or payload), provider_calls=0,
    ))
    return results


# ---------------------------------------------------------------------------
# Case result / summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserSmokeCaseResult:
    case_id: str
    run_id: str
    transport_status: str
    output_status: str | None
    expected_class: str
    raw_expected_status: str
    match: bool
    citations_ok: bool
    citation_detail: str
    latency_ms: int | None
    retry_count: int | None
    provider_model: str | None
    finish_reason: str | None
    error_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    provider_calls: int
    detail: str


@dataclass(frozen=True)
class UserSmokeSummary:
    run_id: str
    case_count: int
    token_written: bool
    completed_at: str
    meta_path: Path
    total_cost_usd: float | None
    cost_known: bool
    failure_path_results: list[FailurePathResult]


def _success_token_gate(
    *,
    selected: list[UserSmokeCase],
    results: list[UserSmokeCaseResult],
    failure_results: list[FailurePathResult],
    cost_known: bool,
    total_cost_usd: float,
    cost_ceiling_usd: float,
    degraded: bool,
    expected_model_id: str,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if len(results) != len(selected):
        reasons.append("case count mismatch")
    if any(not result.match for result in results):
        reasons.append("expected status or evidence mismatch")
    if any(result.transport_status in _FAILED_TRANSPORT for result in results):
        reasons.append("failed transport status")
    if not cost_known:
        reasons.append("cost unknown")
    elif total_cost_usd > cost_ceiling_usd:
        reasons.append(f"cost {total_cost_usd:.4f} exceeds ceiling {cost_ceiling_usd:.2f}")
    if degraded:
        reasons.append("degraded run")
    if any(
        result.provider_model not in (None, "") and result.provider_model != expected_model_id
        for result in results
    ):
        reasons.append("provider/model fallback detected")
    if not failure_results or any(not item.ok for item in failure_results):
        reasons.append("failure-path checks failed")
    if any(item.provider_calls != 0 for item in failure_results):
        reasons.append("failure paths invoked a provider")
    return (not reasons, reasons)


# ---------------------------------------------------------------------------
# Checksums / evidence immutability
# ---------------------------------------------------------------------------


def _write_checksums(directory: Path) -> Path:
    manifest_path = Path(directory) / "checksums.sha256"
    files = sorted(
        path.relative_to(directory).as_posix()
        for path in Path(directory).rglob("*")
        if path.is_file() and path.name != "checksums.sha256"
    )
    lines = [f"{_sha256_file(Path(directory) / rel)}  {rel}\n" for rel in files]
    _atomic_write_text(manifest_path, "".join(lines))
    return manifest_path


def _revalidate_evidence(directory: Path) -> list[str]:
    problems: list[str] = []
    manifest_path = Path(directory) / "checksums.sha256"
    if not manifest_path.is_file():
        return ["checksums.sha256 missing"]
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        digest, _, rel = line.partition("  ")
        path = Path(directory) / rel
        if not path.is_file():
            problems.append(f"missing file {rel}")
        elif _sha256_file(path) != digest:
            problems.append(f"hash mismatch {rel}")
    return problems


def _read_assurance(runtime_db_path: Path, run_id: str) -> RunAssuranceRecord | None:
    conn = sqlite3.connect(str(runtime_db_path))
    try:
        row = conn.execute(
            "SELECT record_json FROM run_assurance WHERE run_id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return RunAssuranceRecord.model_validate_json(row[0])


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def run_user_smoke(
    *,
    cases: list[CasePackCase],
    run_id: str,
    output_root: Path,
    resolved: ResolvedRuntimeIdentity,
    wave2_dir: Path,
    t4_evidence_dir: Path,
    boundary: EmbeddingBoundary,
    runtime_db_path: Path,
    frozen_db_path: Path,
    http: Any,
    provider: str = DEFAULT_PROVIDER,
    model_id: str = DEFAULT_MODEL_ID,
    base_url: str | None = None,
    credential_source: str = "server_env",
    poll_interval_seconds: float = POLL_DEFAULT_INTERVAL_SECONDS,
    poll_timeout_seconds: float = POLL_DEFAULT_TIMEOUT_SECONDS,
    cost_ceiling_usd: float = COST_CEILING_USD,
    provider_validator: Callable[[], Any] | None = None,
    clock: Callable[[], str] | None = None,
) -> UserSmokeSummary:
    """Run the full user-smoke through the public HTTP surface.

    ``cases`` is the full T4 case pack; the three approved cases are selected
    internally.  All gates run before any provider call or artifact write.
    """
    selected = select_user_smoke_cases(cases)
    validate_embedding_boundary(boundary)
    if poll_timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("poll timeout/interval must be positive")
    if cost_ceiling_usd <= 0:
        raise ValueError("cost ceiling must be positive")

    # Gate: Wave 2 evidence (T4 + full four-arm token) before anything else.
    validated_wave2 = validate_wave2_evidence(
        wave2_dir=wave2_dir,
        t4_evidence_dir=t4_evidence_dir,
        current_case_pack=cases,
        resolved=resolved,
    )

    output_root = Path(output_root)
    final_dir = output_root / run_id
    if final_dir.exists():
        raise ValueError(f"run_id already exists: {run_id}")

    staging_dir = output_root / f".{run_id}.staging-{uuid.uuid4().hex[:12]}"
    started_at = clock() if clock is not None else _utc_now()
    try:
        staging_dir.mkdir(parents=True, exist_ok=False)
        for subdir in ("requests", "responses", "traces", "assurance", "failure_paths"):
            (staging_dir / subdir).mkdir(exist_ok=False)

        # Gate: app health readiness.
        _require_ready_health(http)

        # Gate: provider/model validation (one bounded call; adapter seam).
        if provider_validator is not None:
            provider_validator()
        else:
            _validate_provider_http(
                http,
                provider=provider,
                model_id=model_id,
                base_url=base_url,
                credential_source=credential_source,
            )

        request_index = 0
        response_index = 0
        results: list[UserSmokeCaseResult] = []
        total_cost = 0.0
        cost_known = True
        degraded = False

        for item in selected:
            case = item.case
            body = {
                "ticker": case.ticker,
                "trade_date": case.session_date,
                "query": case.query,
                "model": _model_config(
                    provider=provider,
                    model_id=model_id,
                    base_url=base_url,
                    credential_source=credential_source,
                ),
                "config": "mcj_full",
            }
            _atomic_write_json(
                staging_dir / "requests" / f"request-{request_index}.json",
                _redact_payload(body),
            )
            request_index += 1

            submit_resp = http.post("/api/live-runs", json=body)
            _atomic_write_json(
                staging_dir / "responses" / f"response-{response_index}.json",
                _redact_payload(submit_resp.json),
            )
            response_index += 1
            if submit_resp.status_code != 200:
                raise RunnerValidationError(
                    f"live-run submit failed HTTP {submit_resp.status_code}"
                )
            submit_payload = submit_resp.json
            if not isinstance(submit_payload, dict):
                raise RunnerValidationError("live-run submit returned a non-object payload")

            created_run_id = submit_payload.get("run_id")
            transport_status = submit_payload.get("status")
            if transport_status == "FAILED_REQUEST":
                # Typed validation failure at the public boundary (zero provider calls).
                results.append(UserSmokeCaseResult(
                    case_id=case.case_id,
                    run_id=str(created_run_id or ""),
                    transport_status="FAILED_REQUEST",
                    output_status=None,
                    expected_class=item.expected_class,
                    raw_expected_status=item.raw_expected_status,
                    match=False,
                    citations_ok=False,
                    citation_detail="run rejected before provider use",
                    latency_ms=None,
                    retry_count=None,
                    provider_model=None,
                    finish_reason=None,
                    error_reason=str((submit_payload.get("failure") or {}).get("sub_reason") or "failed_request"),
                    input_tokens=None,
                    output_tokens=None,
                    cost_usd=None,
                    provider_calls=0,
                    detail="FAILED_REQUEST",
                ))
                continue
            if not created_run_id:
                raise RunnerValidationError("live-run submit missing run_id")

            # Bounded poll until a terminal state.
            status = transport_status
            deadline = time.monotonic() + poll_timeout_seconds
            while status not in _TERMINAL_STATUSES:
                if time.monotonic() >= deadline:
                    raise RunnerValidationError(
                        f"poll timeout after {poll_timeout_seconds:.1f}s for run {created_run_id}"
                    )
                time.sleep(poll_interval_seconds)
                poll_resp = http.get(f"/api/live-runs/{created_run_id}")
                poll_payload = _require_json_response(
                    poll_resp, endpoint=f"poll live-run {created_run_id}"
                )
                status = poll_payload.get("status", "UNKNOWN")

            events_resp = http.get(f"/api/live-runs/{created_run_id}/events")
            if events_resp.status_code != 200:
                raise RunnerValidationError(
                    f"events endpoint returned HTTP {events_resp.status_code}"
                )
            events = events_resp.json
            if not isinstance(events, list):
                events = []
            artifacts = http.get(f"/api/live-runs/{created_run_id}/artifacts").json
            workspace = _require_json_response(
                http.get(f"/api/live-runs/{created_run_id}/workspace"),
                endpoint=f"workspace {created_run_id}",
            )

            trace_path = staging_dir / "traces" / f"trace-{case.case_id}.json"
            export_run(created_run_id, out_path=trace_path, db_path=runtime_db_path)

            assurance = _read_assurance(runtime_db_path, created_run_id)
            assurance_path = staging_dir / "assurance" / f"assurance-{case.case_id}.json"
            if assurance is not None:
                _atomic_write_json(
                    assurance_path,
                    json.loads(assurance.model_dump_json()),
                )
            else:
                _atomic_write_json(assurance_path, {"error": "assurance record missing"})

            output_status = assurance.output_status if assurance is not None else None
            if output_status is None and isinstance(workspace.get("result"), dict):
                output_status = workspace["result"].get("output_status")

            case_cost, case_cost_known = _aggregate_cost(events)
            if not case_cost_known:
                cost_known = False
            total_cost += case_cost if case_cost is not None else 0.0

            provider_calls = _provider_calls_in_events(events)

            latency_ms = _run_latency_ms(workspace, events)
            retry_count = assurance.retry_count if assurance is not None else None
            tokens_in = sum(int(event.get("input_tokens") or 0) for event in events)
            tokens_out = sum(int(event.get("output_tokens") or 0) for event in events)
            provider_model = next(
                (event.get("model_id") for event in events if event.get("model_id")),
                model_id,
            )
            finish_reason = None
            if isinstance(workspace.get("result"), dict):
                finish_reason = workspace["result"].get("validation_error")
            error_reason = None
            if status in _FAILED_TRANSPORT:
                failure = workspace.get("failure") or {}
                error_reason = str(failure.get("message") or failure.get("sub_reason") or status)

            cited_ids = _cited_ids_from_workspace(workspace)
            citations_ok, citation_detail = _validate_citations(
                frozen_db_path,
                case=case,
                cited_ids=cited_ids,
                manifest_id=resolved.corpus_manifest_id,
            )

            match = (
                status not in _FAILED_TRANSPORT
                and output_status == item.expected_class
                and citations_ok
            )
            if assurance is not None and assurance.is_degraded:
                degraded = True

            results.append(UserSmokeCaseResult(
                case_id=case.case_id,
                run_id=created_run_id,
                transport_status=status,
                output_status=output_status,
                expected_class=item.expected_class,
                raw_expected_status=item.raw_expected_status,
                match=match,
                citations_ok=citations_ok,
                citation_detail=citation_detail,
                latency_ms=latency_ms,
                retry_count=retry_count,
                provider_model=provider_model,
                finish_reason=finish_reason,
                error_reason=error_reason,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cost_usd=case_cost,
                provider_calls=provider_calls,
                detail=f"output_status={output_status} transport={status}",
            ))

        failure_results = _exercise_failure_paths(
            http,
            provider=provider,
            model_id=model_id,
            base_url=base_url,
            credential_source=credential_source,
        )
        _atomic_write_json(
            staging_dir / "failure_paths" / "failure-paths.json",
            {
                "schema_version": FAILURE_PATHS_SCHEMA,
                "results": [asdict(item) for item in failure_results],
            },
        )
        _atomic_write_json(
            staging_dir / "case_results.json",
            {
                "schema_version": CASE_RESULTS_SCHEMA,
                "cases": [asdict(item) for item in results],
            },
        )

        # Secret scan must pass before the token may be written.
        secret_values: tuple[str, ...] = ()
        configured = os.environ.get("DEEPSEEK_API_KEY", "")
        if configured:
            secret_values = (configured,)
        violations = _secret_scan(staging_dir, secret_values=secret_values)
        if violations:
            raise RunnerValidationError(f"secret scan failed: {violations[:5]}")

        may_write, gate_reasons = _success_token_gate(
            selected=selected,
            results=results,
            failure_results=failure_results,
            cost_known=cost_known,
            total_cost_usd=total_cost,
            cost_ceiling_usd=cost_ceiling_usd,
            degraded=degraded,
            expected_model_id=model_id,
        )
        token_written = False
        completed_at = clock() if clock is not None else _utc_now()
        meta: dict[str, Any] = {
            "schema_version": META_SCHEMA_VERSION,
            "run_id": run_id,
            "git_head": resolved.git_head,
            "runtime_git_head": resolved.git_head,
            "index_build_revision": resolved.code_revision,
            "snapshot_id": resolved.snapshot_id,
            "corpus_manifest_id": resolved.corpus_manifest_id,
            "source_bundle_id": resolved.source_bundle_id,
            "probe_report_id": resolved.probe_report_id,
            "postbuild_readiness_id": resolved.postbuild_readiness_id,
            "index_manifest_id": resolved.index_manifest_id,
            "active_table_name": resolved.active_table_name,
            "model_name": resolved.model_name,
            "model_revision": resolved.model_revision,
            "tokenizer_revision": resolved.tokenizer_revision,
            "dimension": resolved.dimension,
            "dtype": resolved.dtype,
            "normalization_mode": resolved.normalization_mode,
            "vector_count": resolved.vector_count,
            "lancedb_row_count": resolved.lancedb_row_count,
            "db_path": str(resolved.db_path),
            "db_sha256": resolved.db_sha256,
            "db_user_version": resolved.db_user_version,
            "db_foreign_key_violations": resolved.db_foreign_key_violations,
            "wave2_evidence_dir": str(validated_wave2.wave2_dir),
            "t4_evidence_dir": str(validated_wave2.t4_evidence.evidence_dir),
            "t4_evidence_case_pack_id": validated_wave2.t4_evidence.case_pack_id,
            "provider": provider,
            "model_id": model_id,
            "credential_source": credential_source,
            "selected_case_ids": list(USER_SMOKE_CASE_IDS),
            "expected_classes": dict(USER_SMOKE_EXPECTED_CLASSES),
            "started_at": started_at,
            "completed_at": completed_at,
            "total_cost_usd": total_cost if cost_known else None,
            "cost_known": cost_known,
            "cost_ceiling_usd": cost_ceiling_usd,
            "poll_timeout_seconds": poll_timeout_seconds,
            "token_gate": {"written": may_write, "reasons": gate_reasons},
        }
        _atomic_write_json(staging_dir / "meta.json", meta)

        if may_write:
            token_path = staging_dir / WAVE_TOKEN_FILENAME
            _atomic_write_text(token_path, USER_SMOKE_TOKEN + "\n")
            token_written = True

        _write_checksums(staging_dir)
        os.replace(staging_dir, final_dir)
        problems = _revalidate_evidence(final_dir)
        if problems:
            raise RunnerValidationError(f"evidence revalidation failed: {problems[:5]}")

        return UserSmokeSummary(
            run_id=run_id,
            case_count=len(results),
            token_written=token_written,
            completed_at=completed_at,
            meta_path=final_dir / "meta.json",
            total_cost_usd=total_cost if cost_known else None,
            cost_known=cost_known,
            failure_path_results=failure_results,
        )
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


__all__ = [
    "COST_CEILING_USD", "DEFAULT_MODEL_ID", "DEFAULT_PROVIDER",
    "FAILURE_PATHS_SCHEMA", "META_SCHEMA_VERSION", "POLL_DEFAULT_INTERVAL_SECONDS",
    "POLL_DEFAULT_TIMEOUT_SECONDS", "USER_SMOKE_CASE_IDS", "USER_SMOKE_EXPECTED_CLASSES",
    "USER_SMOKE_TOKEN", "WAVE2_FOUR_ARM_TOKEN", "WAVE_TOKEN_FILENAME",
    "CASE_RESULTS_SCHEMA", "EXPECTED_WAVE2_CASE_COUNT",
    "FailurePathResult", "HttpResponse", "UserSmokeCase", "UserSmokeCaseResult",
    "UserSmokeSummary", "ValidatedWave2Evidence",
    "_redact_payload", "_secret_scan", "normalize_expected_class",
    "run_user_smoke", "select_user_smoke_cases", "validate_wave2_evidence",
]
