"""M8-B contract tests: pointer-free candidate-FTS recovery retrieval."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from catalyst_eval.v1_1.case import GoldenCase
from catalyst_eval.v1_1.recovery_retrieval import (
    evaluate_recovery_retrieval_gates,
    frozen_ranked_payload,
    rank_cases,
    run_candidate_fts_retrieval,
)

from tests.v1_1_fixtures import make_case

BUILD_ID = "b" * 64
MANIFEST_ID = "c" * 64


def _case(case_id: str, *, primary: tuple[str, ...], evidence: tuple[str, ...]):
    return GoldenCase.model_validate(
        make_case(
            case_id=case_id,
            ticker="AAPL",
            session_date="2025-05-02",
            cutoff="2025-05-02T20:00:00Z",
            question=f"why did AAPL move on {case_id}?",
            oracle_status="SUFFICIENT",
            expected_primary_evidence=primary,
            evidence_ids=evidence,
        )
    )


def _conn(tmp_path: Path, chunk_ids: dict[str, str]) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "derivative.db")
    conn.execute(
        "CREATE TABLE corpus_build_chunks (build_id TEXT, chunk_id TEXT, "
        "content_state TEXT, content_text TEXT)"
    )
    for chunk_id, state in chunk_ids.items():
        conn.execute(
            "INSERT INTO corpus_build_chunks VALUES (?,?,?,?)",
            (BUILD_ID, chunk_id, state, "body" if state == "FULL_TEXT" else None),
        )
    conn.commit()
    return conn


def test_retrieval_never_receives_gold_fields_and_included_ids_are_citable(tmp_path):
    cases = [
        _case("c01", primary=("e-primary",), evidence=("e-primary", "e-secondary")),
        _case("c02", primary=(), evidence=()),
    ]
    conn = _conn(
        tmp_path,
        {"e-primary": "FULL_TEXT", "e-secondary": "METADATA_ONLY"},
    )
    captured: list[dict] = []

    def retrieve(query):
        captured.append(dict(query))
        return ("e-primary", "e-secondary", "e-missing")

    try:
        ranked = rank_cases(
            cases=cases,
            retrieve=retrieve,
            conn=conn,
            corpus_manifest_id=MANIFEST_ID,
            build_id=BUILD_ID,
        )
        assert [case.case_id for case in ranked] == ["c01", "c02"]
        for query in captured:
            assert set(query) <= {
                "case_id", "question", "ticker", "cutoff", "session_date"
            }
            assert "expected_primary_evidence" not in query
            assert "evidence_judgments" not in query
        assert captured[0]["question"] == "why did AAPL move on c01?"
        assert ranked[0].included_evidence_ids == ("e-primary",)
        assert ranked[0].citable_body_count == 1
        assert ranked[0].ranked_evidence_ids == (
            "e-primary", "e-secondary", "e-missing",
        )
    finally:
        conn.close()


def test_frozen_pools_are_written_before_scoring(tmp_path):
    cases = [_case("c01", primary=("e-primary",), evidence=("e-primary",))]
    conn = _conn(tmp_path, {"e-primary": "FULL_TEXT"})
    frozen = tmp_path / "ranked.json"

    def retrieve(query):
        return ("e-primary",)

    try:
        report = run_candidate_fts_retrieval(
            db_path=tmp_path / "derivative.db",
            corpus_manifest_id=MANIFEST_ID,
            build_id=BUILD_ID,
            cases=cases,
            retrieve=retrieve,
            frozen_ranked_path=frozen,
        )
        payload = json.loads(frozen.read_text(encoding="utf-8"))
        assert payload["cases"][0]["ranked_evidence_ids"] == ["e-primary"]
        assert "expected_primary_evidence" not in frozen.read_text(encoding="utf-8")
        assert "gold" not in frozen.read_text(encoding="utf-8")
    finally:
        conn.close()
    assert report.gate_status["required_primary_citable"] == "PASS"
    assert report.cases[0].expected_primary is True


def test_empty_denominator_is_not_exercised_and_fails(tmp_path):
    cases = [_case("c01", primary=(), evidence=())]
    conn = _conn(tmp_path, {})
    try:
        report = run_candidate_fts_retrieval(
            db_path=tmp_path / "derivative.db",
            corpus_manifest_id=MANIFEST_ID,
            build_id=BUILD_ID,
            cases=cases,
            retrieve=lambda query: (),
        )
    finally:
        conn.close()
    assert report.gate_status["recall_at_8"] == "NOT_EXERCISED"
    assert report.gate_status["primary_source_hit"] == "NOT_EXERCISED"
    assert report.gate_status["required_primary_citable"] == "NOT_EXERCISED"
    assert report.gate_passed is False


def test_gate_thresholds_and_violations():
    cases = [
        _case("c01", primary=("e1",), evidence=("e1", "e2")),
        _case("c02", primary=("e3",), evidence=("e3",)),
    ]
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE corpus_build_chunks (build_id TEXT, chunk_id TEXT, "
        "content_state TEXT, content_text TEXT)"
    )
    for chunk_id in ("e1", "e2", "e3"):
        conn.execute(
            "INSERT INTO corpus_build_chunks VALUES (?,?,?,?)",
            (BUILD_ID, chunk_id, "FULL_TEXT", "body"),
        )
    conn.commit()
    try:
        ranked = rank_cases(
            cases=cases,
            retrieve=lambda query: ("e1", "e2", "e3"),
            conn=conn,
            corpus_manifest_id=MANIFEST_ID,
            build_id=BUILD_ID,
        )
    finally:
        conn.close()
    ranked = tuple(
        type(case)(
            **{
                **case.__dict__,
                "expected_primary": True,
            }
        )
        for case in ranked
    )
    from catalyst_eval.v1_1.retrieval_metrics import (
        RetrievalResult,
        compute_retrieval_metrics,
    )

    metrics = compute_retrieval_metrics(
        [
            RetrievalResult(
                case_id=case.case_id,
                pool=case.pool,
                ranked_evidence_ids=case.ranked_evidence_ids,
            )
            for case in ranked
        ],
        cases,
    )
    status = evaluate_recovery_retrieval_gates(ranked, metrics)
    assert status["recall_at_8"] == "PASS"
    assert status["primary_source_hit"] == "PASS"
    assert status["ticker_or_cutoff_violations"] == "PASS"
    assert frozen_ranked_payload(ranked)["cases"][0]["case_id"] == "c01"


def test_benchmark_cases_fixture_covers_twelve_cases():
    from tests.v1_1_fixtures import make_stage1_cases

    assert len(make_stage1_cases()) == 12
