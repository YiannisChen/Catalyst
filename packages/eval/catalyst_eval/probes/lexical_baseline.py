"""Deterministic 12-case lexical baseline for the promoted Pre-B6 corpus."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Mapping

from catalyst_data.manifests.universe import sha256_identity
from catalyst_data.pre_b6_probes import (
    load_and_verify_postbuild_readiness_report,
    load_and_verify_probe_report,
    verify_probe_report_against_db,
)
from catalyst_data.retrieval.fts5 import retrieve_lexical


BASELINE_SCHEMA_VERSION = "pre_b6_lexical_baseline_v1"
EXPECTED_CASES = (
    ("c01", "single_source_answerable", "AAPL"),
    ("c02", "single_source_answerable", "MSFT"),
    ("c03", "single_source_answerable", "NVDA"),
    ("c04", "single_source_answerable", "JPM"),
    ("c05", "multi_source_answerable", "TSLA"),
    ("c06", "multi_source_answerable", "META"),
    ("c07", "correct_abstain", "AMD"),
    ("c08", "correct_abstain", "GOOGL"),
    ("c09", "unsupported_distractor", "AMZN"),
    ("c10", "unsupported_distractor", "UNH"),
    ("c11", "temporal_lookahead_trap", "ORCL"),
    ("c12", "temporal_lookahead_trap", "CRM"),
)
EXPECTED_SLOTS = {slot for _, slot, _ in EXPECTED_CASES}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_case_pack(path: Path) -> list[dict[str, str]]:
    path = Path(path)
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid case JSON at line {line_number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"case at line {line_number} must be an object")
            if set(row) != {"case_id", "slot", "ticker"}:
                raise ValueError(f"case at line {line_number} has invalid fields")
            rows.append({key: str(row[key]) for key in ("case_id", "slot", "ticker")})
    expected = [
        {"case_id": case_id, "slot": slot, "ticker": ticker}
        for case_id, slot, ticker in EXPECTED_CASES
    ]
    if rows != expected:
        raise ValueError("12-case pack order, slot, or ticker drift")
    if len({row["case_id"] for row in rows}) != 12:
        raise ValueError("12-case pack contains duplicate case IDs")
    if any(row["slot"] not in EXPECTED_SLOTS for row in rows):
        raise ValueError("12-case pack contains an unknown slot")
    return rows


def build_lexical_baseline_id(
    body: Mapping[str, Any],
    *,
    created_at: str | None = None,
    output_path: str | None = None,
) -> str:
    identity = dict(body)
    identity.pop("baseline_id", None)
    identity.pop("created_at", None)
    identity.pop("output_path", None)
    return sha256_identity(identity)


def _result_body(result: Any) -> dict[str, Any]:
    return {
        "chunk_id": result.chunk_id,
        "document_id": result.document_id,
        "rank": result.lexical_rank,
        "lexical_raw_score": result.lexical_raw_score,
        "source_class": result.source_class,
        "available_at": result.available_at,
        "corpus_manifest_id": result.corpus_manifest_id,
        "index_manifest_id": result.index_manifest_id,
        "mode_served": result.mode_served,
        "is_degraded": result.is_degraded,
        "fallback_reason": result.fallback_reason,
    }


def _atomic_write(path: Path, body: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def freeze_lexical_baseline(
    conn: sqlite3.Connection,
    *,
    universe_manifest_id: str,
    snapshot_id: str,
    corpus_manifest_id: str,
    probe_report_path: Path,
    postbuild_readiness_report_path: Path,
    case_pack_path: Path,
    output_path: Path,
) -> tuple[str, dict[str, Any]]:
    cases = load_case_pack(case_pack_path)
    postbuild = load_and_verify_postbuild_readiness_report(
        conn,
        Path(postbuild_readiness_report_path),
        expected_universe_manifest_id=universe_manifest_id,
        expected_snapshot_id=snapshot_id,
        expected_corpus_manifest_id=corpus_manifest_id,
    )
    probe = load_and_verify_probe_report(
        Path(probe_report_path),
        expected_snapshot_id=snapshot_id,
        expected_corpus_manifest_id=corpus_manifest_id,
        expected_universe_manifest_id=universe_manifest_id,
        expected_postbuild_readiness_id=postbuild["postbuild_readiness_id"],
    )
    verify_probe_report_against_db(
        conn,
        probe,
        expected_universe_manifest_id=universe_manifest_id,
        expected_snapshot_id=snapshot_id,
        expected_corpus_manifest_id=corpus_manifest_id,
        expected_probe_cutoff=probe["probe_cutoff"],
        postbuild_readiness_report_path=Path(postbuild_readiness_report_path),
    )
    lexical_by_ticker = {
        row["ticker"]: row for row in probe["lexical_results"]
    }
    expected_tickers = {ticker for _, _, ticker in EXPECTED_CASES}
    if not expected_tickers.issubset(lexical_by_ticker):
        raise ValueError("certified probe report is missing a 12-case ticker")

    manifest_row = conn.execute(
        "SELECT manifest_json FROM corpus_manifest WHERE manifest_id=? AND is_current=1",
        (corpus_manifest_id,),
    ).fetchone()
    if manifest_row is None:
        raise ValueError("current corpus manifest missing")
    manifest = json.loads(manifest_row[0] or "{}")
    tokenizer = {
        key: manifest.get(key)
        for key in (
            "tokenizer_model_id",
            "tokenizer_revision",
            "chunk_profile_versions",
            "normalization_version",
            "source_classifier_version",
        )
    }

    baseline_cases: list[dict[str, Any]] = []
    total_look_ahead = 0
    for case in cases:
        smoke = lexical_by_ticker[case["ticker"]]
        if smoke.get("ok") is not True or smoke.get("mode_served") != "fts5":
            raise ValueError(f"certified lexical smoke missing for {case['ticker']}")
        terms = smoke.get("query_terms")
        cutoff = smoke.get("cutoff")
        if not isinstance(terms, list) or not terms or not isinstance(cutoff, str):
            raise ValueError(f"certified lexical anchor incomplete for {case['ticker']}")
        retrieved = retrieve_lexical(
            conn,
            " ".join(str(term) for term in terms),
            ticker=case["ticker"],
            cutoff=cutoff,
            requested_manifest_id=corpus_manifest_id,
            candidate_depth=20,
            top_k=20,
        )
        if retrieved.mode_served != "fts5" or retrieved.is_degraded:
            raise ValueError(f"lexical baseline requires fts5 for {case['ticker']}")
        results = [_result_body(result) for result in retrieved.results]
        future = sum(1 for result in results if result["available_at"] > cutoff)
        if future:
            raise ValueError(f"look-ahead detected for {case['ticker']}")
        total_look_ahead += future
        baseline_cases.append(
            {
                **case,
                "query_terms": list(terms),
                "anchor_chunk_id": smoke["anchor_chunk_id"],
                "anchor_document_id": smoke["anchor_document_id"],
                "cutoff": cutoff,
                "candidate_depth": 20,
                "top_k": 20,
                "mode_served": retrieved.mode_served,
                "results": results,
                "look_ahead": 0,
                "look_ahead_count": future,
            }
        )

    body: dict[str, Any] = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "corpus_manifest_id": corpus_manifest_id,
        "universe_manifest_id": universe_manifest_id,
        "probe_report_id": probe["probe_report_id"],
        "postbuild_readiness_id": postbuild["postbuild_readiness_id"],
        "probe_cutoff": probe["probe_cutoff"],
        "case_pack_sha256": _sha256_file(Path(case_pack_path)),
        "case_count": len(baseline_cases),
        "look_ahead": 0,
        "look_ahead_count": total_look_ahead,
        "tokenizer": tokenizer,
        "cases": baseline_cases,
    }
    baseline_id = build_lexical_baseline_id(body)
    body["baseline_id"] = baseline_id
    output_path = Path(output_path)
    if output_path.exists():
        try:
            with output_path.open("r", encoding="utf-8") as handle:
                existing = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("existing lexical baseline is invalid") from exc
        if existing != body:
            raise ValueError("existing lexical baseline conflicts with promoted identity")
    else:
        _atomic_write(output_path, body)
    return baseline_id, body


__all__ = [
    "BASELINE_SCHEMA_VERSION",
    "EXPECTED_CASES",
    "build_lexical_baseline_id",
    "freeze_lexical_baseline",
    "load_case_pack",
]
