"""T4 served-corpus existence probe (read-only, fail-closed N/N gate).

The predicate is the manager-approved served-corpus query:
status='active' AND eligibility='eligible' AND manifest_id = current corpus
manifest AND available_at <= cutoff AND ticker present in ticker_associations
via json_each. On the frozen DB the relation resolves to the
``corpus_served_chunks`` view; test fixtures fall back to ``corpus_chunks``
through the same production primitive.

Amendment P5: the T4 gate file is ``T4_PROBE_TOKEN.txt`` with content
``T4_PROBE_OK``. ``WAVE_TOKEN.txt`` is never written by the T4 probe (Wave-2
gates are owned by the four-arm runner).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalyst_data.corpus.streaming_publication import served_chunks_relation

from .case_pack import CasePackCase

SCHEMA_VERSION = "served_corpus_probe_v1"
T4_PROBE_TOKEN = "T4_PROBE_OK"
T4_PROBE_TOKEN_FILENAME = "T4_PROBE_TOKEN.txt"


@dataclass(frozen=True)
class CaseProbeResult:
    case_id: str
    ticker: str
    cutoff: str
    count: int

    @property
    def ok(self) -> bool:
        return self.count >= 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "ticker": self.ticker,
            "cutoff": self.cutoff,
            "count": self.count,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class ServedCorpusProbeReport:
    schema_version: str
    corpus_manifest_id: str
    case_count: int
    passed_count: int
    all_passed: bool
    per_case: tuple[CaseProbeResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "corpus_manifest_id": self.corpus_manifest_id,
            "case_count": self.case_count,
            "passed_count": self.passed_count,
            "all_passed": self.all_passed,
            "per_case": [result.to_dict() for result in self.per_case],
        }


def run_served_corpus_probe(
    conn: sqlite3.Connection,
    *,
    corpus_manifest_id: str,
    cases: list[CasePackCase],
) -> ServedCorpusProbeReport:
    relation = served_chunks_relation(conn)
    per_case: list[CaseProbeResult] = []
    for case in cases:
        count = conn.execute(
            f"""SELECT COUNT(*) FROM {relation} c
                WHERE c.manifest_id = ?
                  AND c.status = 'active'
                  AND c.eligibility = 'eligible'
                  AND c.available_at <= ?
                  AND EXISTS (
                    SELECT 1 FROM json_each(c.ticker_associations) je
                    WHERE je.value = ?
                  )""",
            (corpus_manifest_id, case.cutoff, case.ticker),
        ).fetchone()[0]
        per_case.append(CaseProbeResult(case.case_id, case.ticker, case.cutoff, int(count)))
    passed = sum(1 for result in per_case if result.ok)
    return ServedCorpusProbeReport(
        schema_version=SCHEMA_VERSION,
        corpus_manifest_id=corpus_manifest_id,
        case_count=len(per_case),
        passed_count=passed,
        all_passed=passed == len(per_case) and len(per_case) > 0,
        per_case=tuple(per_case),
    )


def _atomic_write(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_probe_evidence(
    report: ServedCorpusProbeReport,
    *,
    run_dir: Path,
    db_sha256: str,
    corpus_manifest_id: str,
    case_pack_id: str,
    case_pack_path: str,
    runtime_git_head: str,
    index_build_code_revision: str,
    snapshot_id: str,
    source_bundle_id: str,
    probe_report_id: str,
    postbuild_readiness_id: str,
    index_manifest_id: str,
    db_path: str,
    db_user_version: int,
    db_foreign_key_violations: int,
    lancedb_dir: str,
    active_table_name: str,
    model_name: str,
    model_revision: str,
    tokenizer_revision: str,
    dimension: int,
    dtype: str,
    normalization_mode: str,
    embedding_mode: str,
) -> Path:
    if not report.all_passed:
        raise ValueError(
            f"served-corpus probe failed N/N: {report.passed_count}/{report.case_count}"
        )
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    probe_body = {
        "schema_version": SCHEMA_VERSION,
        "corpus_manifest_id": corpus_manifest_id,
        "db_sha256": db_sha256,
        "case_count": report.case_count,
        "passed_count": report.passed_count,
        "all_passed": True,
        "per_case": [result.to_dict() for result in report.per_case],
        "query_predicate": (
            "status='active' AND eligibility='eligible' AND manifest_id=current "
            "AND available_at <= cutoff AND ticker in ticker_associations (json_each)"
        ),
    }
    probe_path = run_dir / "probe_report.json"
    _atomic_write(probe_path, probe_body)
    probe_sha256 = hashlib.sha256(probe_path.read_bytes()).hexdigest()

    started_at = datetime.now(timezone.utc).isoformat()
    completed_at = datetime.now(timezone.utc).isoformat()
    meta: dict[str, Any] = {
        "schema_version": "t4_probe_meta_v1",
        "task": "T4",
        "phase": "wave2_preparation",
        "started_at": started_at,
        "completed_at": completed_at,
        "runtime_git_head": runtime_git_head,
        "index_build_code_revision": index_build_code_revision,
        "snapshot_id": snapshot_id,
        "corpus_manifest_id": corpus_manifest_id,
        "source_bundle_id": source_bundle_id,
        "probe_report_id": probe_report_id,
        "postbuild_readiness_id": postbuild_readiness_id,
        "index_manifest_id": index_manifest_id,
        "db_path": db_path,
        "db_sha256": db_sha256,
        "db_user_version": db_user_version,
        "db_foreign_key_violations": db_foreign_key_violations,
        "lancedb_dir": lancedb_dir,
        "active_table_name": active_table_name,
        "model_name": model_name,
        "model_revision": model_revision,
        "tokenizer_revision": tokenizer_revision,
        "dimension": dimension,
        "dtype": dtype,
        "normalization_mode": normalization_mode,
        "embedding_mode": embedding_mode,
        "case_pack_id": case_pack_id,
        "case_pack_path": case_pack_path,
        "case_count": report.case_count,
        "passed_count": report.passed_count,
        "probe_report_path": "probe_report.json",
        "probe_report_sha256": probe_sha256,
        "nn_result": f"{report.passed_count}/{report.case_count}",
        "per_case_summary": [result.to_dict() for result in report.per_case],
    }
    _atomic_write(run_dir / "meta.json", meta)
    _atomic_write_text(run_dir / T4_PROBE_TOKEN_FILENAME, T4_PROBE_TOKEN + "\n")
    return run_dir / T4_PROBE_TOKEN_FILENAME


__all__ = [
    "SCHEMA_VERSION", "T4_PROBE_TOKEN", "T4_PROBE_TOKEN_FILENAME",
    "CaseProbeResult", "ServedCorpusProbeReport", "run_served_corpus_probe",
    "write_probe_evidence",
]
