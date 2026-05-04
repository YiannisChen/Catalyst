"""Helpers for frozen-set eval runs and threshold calibration."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from importlib import metadata as importlib_metadata
import json
from pathlib import Path
import subprocess
from typing import Any, Iterable


SCHEMA_VERSION = "1.0"
GEO_CORPUS_TIER_P0 = 2
W15_LANCEDB_SENTINEL = "DEFERRED_P1"
DEFAULT_RANDOM_SEED = 42
CURRENT_THRESHOLDS = {"K_sufficient": 4, "K_partial": 2, "M_threshold": 0.6}


@dataclass(frozen=True)
class ThresholdCandidate:
    K_sufficient: int
    K_partial: int
    M_threshold: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "K_sufficient": self.K_sufficient,
            "K_partial": self.K_partial,
            "M_threshold": self.M_threshold,
        }


def load_jsonl(path: Path | str) -> list[dict[str, Any]]:
    source = Path(path)
    return [json.loads(line) for line in source.read_text().splitlines() if line.strip()]


def build_case_distribution(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    materialized = list(rows)
    return {
        "sufficient": sum(1 for row in materialized if row.get("expected_status") == "SUFFICIENT"),
        "partial": sum(1 for row in materialized if row.get("expected_status") == "PARTIAL"),
        "should_refuse": sum(1 for row in materialized if bool(row.get("should_refuse"))),
    }


def _predict_status(
    evidence_count: int,
    magnitude_coverage: float,
    *,
    candidate: ThresholdCandidate,
) -> str:
    if evidence_count >= candidate.K_sufficient and magnitude_coverage >= candidate.M_threshold:
        return "SUFFICIENT"
    if evidence_count == 0:
        return "INSUFFICIENT"
    if evidence_count >= candidate.K_partial or magnitude_coverage < candidate.M_threshold:
        return "PARTIAL"
    return "INSUFFICIENT"


def _candidate_window(current: dict[str, float | int]) -> list[ThresholdCandidate]:
    base_k_sufficient = int(current["K_sufficient"])
    base_k_partial = int(current["K_partial"])
    base_m = float(current["M_threshold"])

    m_values = sorted({round(max(0.0, min(1.0, base_m + delta)), 2) for delta in (-0.1, 0.0, 0.1)})
    candidates: list[ThresholdCandidate] = []
    for k_sufficient in range(max(1, base_k_sufficient - 1), base_k_sufficient + 2):
        for k_partial in range(max(1, base_k_partial - 1), base_k_partial + 2):
            if k_partial >= k_sufficient:
                continue
            for m_threshold in m_values:
                candidates.append(
                    ThresholdCandidate(
                        K_sufficient=k_sufficient,
                        K_partial=k_partial,
                        M_threshold=m_threshold,
                    )
                )
    return candidates


def _strictness_key(candidate: ThresholdCandidate) -> tuple[int, int, float]:
    return (candidate.K_sufficient, -candidate.K_partial, candidate.M_threshold)


def _distance_from_current(candidate: ThresholdCandidate, current: ThresholdCandidate) -> tuple[float, float, float]:
    return (
        abs(candidate.K_sufficient - current.K_sufficient),
        abs(candidate.K_partial - current.K_partial),
        abs(candidate.M_threshold - current.M_threshold),
    )


def calibrate_thresholds(
    observations: list[dict[str, Any]],
    *,
    current: dict[str, float | int] | None = None,
) -> dict[str, Any]:
    current = current or CURRENT_THRESHOLDS
    scored: list[tuple[float, ThresholdCandidate]] = []
    for candidate in _candidate_window(current):
        matches = 0
        for observation in observations:
            predicted = _predict_status(
                int(observation.get("evidence_count", 0) or 0),
                float(observation.get("magnitude_coverage", 0.0) or 0.0),
                candidate=candidate,
            )
            if predicted == str(observation.get("expected_status")):
                matches += 1
        accuracy = matches / len(observations) if observations else 0.0
        scored.append((accuracy, candidate))

    best_accuracy = max((accuracy for accuracy, _ in scored), default=0.0)
    top_candidates = [candidate for accuracy, candidate in scored if accuracy == best_accuracy]
    current_candidate = ThresholdCandidate(
        K_sufficient=int(current["K_sufficient"]),
        K_partial=int(current["K_partial"]),
        M_threshold=round(float(current["M_threshold"]), 2),
    )
    closest_candidates = [
        candidate
        for candidate in top_candidates
        if _distance_from_current(candidate, current_candidate)
        == min(_distance_from_current(other, current_candidate) for other in top_candidates)
    ]
    chosen = max(closest_candidates, key=_strictness_key)
    current_is_best = chosen == current_candidate
    rationale_prefix = "Selected the strictest top-scoring candidate"
    rationale_suffix = " (matches current defaults)." if current_is_best else "."

    return {
        "window": {
            "K_sufficient": [int(current["K_sufficient"]) - 1, int(current["K_sufficient"]) + 1],
            "K_partial": [int(current["K_partial"]) - 1, int(current["K_partial"]) + 1],
            "M_threshold": [round(float(current["M_threshold"]) - 0.1, 2), round(float(current["M_threshold"]) + 0.1, 2)],
        },
        "chosen": chosen.as_dict(),
        "chosen_accuracy": best_accuracy,
        "candidate_count": len(scored),
        "rationale": (
            f"{rationale_prefix} within the OD-1 calibration window at accuracy={best_accuracy:.2f}"
            f"{rationale_suffix}"
        ),
    }


def sha256_file(path: Path | str) -> str:
    target = Path(path)
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def latest_freeze_header(path: Path | str = "data/eval_reports") -> dict[str, Any]:
    out_dir = Path(path)
    latest = sorted(out_dir.glob("freeze_header_*.json"))[-1]
    return json.loads(latest.read_text())


def provider_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for package_name in ("anthropic", "langchain-anthropic"):
        try:
            versions[package_name.replace("-", "_")] = importlib_metadata.version(package_name)
        except importlib_metadata.PackageNotFoundError:
            versions[package_name.replace("-", "_")] = None
    return versions


def git_sha(cwd: Path | str = ".") -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=str(cwd),
        text=True,
    ).strip()


def build_report_header(
    *,
    frozen_ts: str,
    db_path: Path | str,
    lancedb_dir_sha256: str,
    model_id_per_role: dict[str, str],
    random_seed: int,
    case_distribution: dict[str, int],
    provider_version: dict[str, str | None] | None = None,
    direct_llm_model_substitution: dict[str, Any] | None = None,
    cwd: Path | str = ".",
) -> dict[str, Any]:
    header = {
        "frozen_ts": frozen_ts,
        "model_id_per_role": model_id_per_role,
        "provider_version": provider_version if provider_version is not None else provider_versions(),
        "db_sha256": sha256_file(db_path),
        "lancedb_dir_sha256": lancedb_dir_sha256,
        "code_git_sha": git_sha(cwd),
        "random_seed": random_seed,
        "case_distribution": case_distribution,
        "geo_corpus_tier": GEO_CORPUS_TIER_P0,
    }
    if direct_llm_model_substitution is not None:
        header["direct_llm_model_substitution"] = direct_llm_model_substitution
    return header


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
