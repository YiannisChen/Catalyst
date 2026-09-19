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
LEXICAL_DIGEST = "f" * 64


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


def _conn(
    tmp_path: Path,
    chunk_ids: dict[str, str],
    *,
    available_at: str = "2025-05-01T12:00:00Z",
    eligible: bool = True,
) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "derivative.db")
    conn.execute("DROP TABLE IF EXISTS corpus_build_chunks")
    conn.execute("DROP TABLE IF EXISTS corpus_publication_builds")
    conn.execute(
        "CREATE TABLE corpus_build_chunks (build_id TEXT, chunk_id TEXT, "
        "content_state TEXT, content_text TEXT, available_at TEXT, "
        "ticker_associations TEXT, status TEXT, eligibility TEXT)"
    )
    conn.execute(
        "CREATE TABLE corpus_publication_builds (build_id TEXT, lexical_digest TEXT)"
    )
    conn.execute(
        "INSERT INTO corpus_publication_builds VALUES (?,?)",
        (BUILD_ID, LEXICAL_DIGEST),
    )
    for chunk_id, state in chunk_ids.items():
        conn.execute(
            "INSERT INTO corpus_build_chunks VALUES (?,?,?,?,?,?,?,?)",
            (
                BUILD_ID,
                chunk_id,
                state,
                "body" if state == "FULL_TEXT" else None,
                available_at,
                '["AAPL"]',
                "active",
                "eligible" if eligible else "ineligible",
            ),
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


def test_ranked_case_violations_are_filled_from_the_returned_hits(tmp_path):
    from catalyst_eval.v1_1.recovery_retrieval import CandidateHit

    cases = [_case("c01", primary=("e1",), evidence=("e1",))]
    conn = _conn(tmp_path, {"e1": "FULL_TEXT"})
    try:
        ranked = rank_cases(
            cases=cases,
            retrieve=lambda query: (
                CandidateHit(chunk_id="e1"),
                CandidateHit(
                    chunk_id="e-late", cutoff_eligible=False
                ),
                CandidateHit(chunk_id="e-other-ticker", ticker_eligible=False),
            ),
            conn=conn,
            corpus_manifest_id=MANIFEST_ID,
            build_id=BUILD_ID,
        )
    finally:
        conn.close()
    case = ranked[0]
    assert case.ranked_evidence_ids == ("e1", "e-late", "e-other-ticker")
    assert case.cutoff_violations == ("e-late",)
    assert case.ticker_violations == ("e-other-ticker",)
    assert case.ticker_violations and case.cutoff_violations


def test_fts_digest_is_the_candidate_lexical_digest(tmp_path):
    from catalyst_eval.v1_1.recovery_retrieval import RecoveryRetrievalError

    cases = [_case("c01", primary=("e-primary",), evidence=("e-primary",))]
    conn = _conn(tmp_path, {"e-primary": "FULL_TEXT"})
    try:
        report = run_candidate_fts_retrieval(
            db_path=tmp_path / "derivative.db",
            corpus_manifest_id=MANIFEST_ID,
            build_id=BUILD_ID,
            cases=cases,
            retrieve=lambda query: ("e-primary",),
        )
        assert report.fts_digest == LEXICAL_DIGEST
        assert report.as_dict()["fts_digest"] == LEXICAL_DIGEST
    finally:
        conn.close()

    conn = _conn(tmp_path, {"e-primary": "FULL_TEXT"})
    try:
        conn.execute("UPDATE corpus_publication_builds SET lexical_digest=''")
        conn.commit()
        try:
            run_candidate_fts_retrieval(
                db_path=tmp_path / "derivative.db",
                corpus_manifest_id=MANIFEST_ID,
                build_id=BUILD_ID,
                cases=cases,
                retrieve=lambda query: ("e-primary",),
            )
        except RecoveryRetrievalError as exc:
            assert "lexical_digest" in str(exc)
        else:  # pragma: no cover - fail closed is required
            raise AssertionError("missing candidate lexical_digest must fail closed")
    finally:
        conn.close()


def _selection_conn(tmp_path: Path, rows: tuple[tuple[str, str, str, str], ...]):
    """Build-scoped chunk table keyed by (document_id, ordinal)."""
    conn = sqlite3.connect(tmp_path / "selection.db")
    conn.execute("DROP TABLE IF EXISTS corpus_build_chunks")
    conn.execute(
        "CREATE TABLE corpus_build_chunks (build_id TEXT, chunk_id TEXT, "
        "document_id TEXT, ordinal TEXT, content_state TEXT, content_text TEXT)"
    )
    for ordinal, document_id, state, text in rows:
        conn.execute(
            "INSERT INTO corpus_build_chunks VALUES (?,?,?,?,?,?)",
            (
                BUILD_ID,
                f"{document_id}:filing_v3:unknown_000:{ordinal}",
                document_id,
                ordinal,
                state,
                text,
            ),
        )
    conn.commit()
    return conn


def test_within_document_slots_are_full_text_scored_and_capped(tmp_path):
    """The window identifies documents; the displayed ordinal is scored."""
    from catalyst_eval.v1_1.within_document_selection import (
        MAX_CHUNKS_PER_DOCUMENT,
        select_within_document_slots,
    )

    conn = _selection_conn(
        tmp_path,
        (
            # Cover page / XBRL boilerplate of the matching document.
            ("0001", "doc-a", "FULL_TEXT", "us-gaap:Revenues xbrl cover page"),
            ("0002", "doc-a", "METADATA_ONLY", "quarter results"),
            ("0003", "doc-a", "FULL_TEXT", "Item 2.02 results of operations"),
            ("0004", "doc-a", "FULL_TEXT", "total revenue and net income table"),
            ("0005", "doc-a", "FULL_TEXT", "diluted earnings per share"),
            ("0006", "doc-a", "FULL_TEXT", "revenue revenue revenue"),
            # Another document in the window, entirely boilerplate.
            ("0001", "doc-b", "FULL_TEXT", "Exchange Act of 1934 cover page"),
        ),
    )
    try:
        window = (
            "doc-a:filing_v3:unknown_000:0001",
            "doc-b:filing_v3:unknown_000:0001",
            "doc-a:filing_v3:unknown_000:0002",
        )
        assert select_within_document_slots(
            conn, build_id=BUILD_ID, candidate_chunk_ids=window, top_k=8
        ) == (
            # Scored by public text (score 26 / 21 / 18), capped at 3 per doc.
            "doc-a:filing_v3:unknown_000:0004",
            "doc-a:filing_v3:unknown_000:0005",
            "doc-a:filing_v3:unknown_000:0003",
        )
        assert MAX_CHUNKS_PER_DOCUMENT == 3
    finally:
        conn.close()


def test_within_document_slots_never_pad_with_unscored_siblings(tmp_path):
    """An empty slot beats a chunk with no positive public signal."""
    from catalyst_eval.v1_1.within_document_selection import (
        select_within_document_slots,
    )

    conn = _selection_conn(
        tmp_path,
        (
            ("0001", "doc-a", "FULL_TEXT", "quarter ends on 2025-05-01"),
            ("0002", "doc-a", "FULL_TEXT", "net income and diluted earnings"),
            ("0003", "doc-a", "FULL_TEXT", "unrelated narrative"),
        ),
    )
    try:
        window = ("doc-a:filing_v3:unknown_000:0001",)
        assert select_within_document_slots(
            conn, build_id=BUILD_ID, candidate_chunk_ids=window, top_k=8
        ) == ("doc-a:filing_v3:unknown_000:0002",)
        assert select_within_document_slots(
            conn, build_id=BUILD_ID, candidate_chunk_ids=(), top_k=8
        ) == ()
    finally:
        conn.close()


def test_required_primary_cases_are_the_nine_gold_cases():
    """The required-primary cohort is c01/c02/c05-c11 (never c03/c04/c12)."""
    from catalyst_eval.v1_1.loader import load_benchmark_cases

    stage1 = Path(__file__).resolve().parents[1] / "benchmarks" / "v1_1" / "stage1"
    manifest = json.loads((stage1 / "manifest.json").read_text(encoding="utf-8"))
    cases = load_benchmark_cases(stage1 / "cases.jsonl", manifest=manifest)
    required = {
        case.case_id for case in cases if case.expected_primary_evidence
    }
    assert required == {
        "c01", "c02", "c05", "c06", "c07", "c08", "c09", "c10", "c11",
    }
    assert not required & {"c03", "c04", "c12"}


def test_default_retrieve_rejects_a_shallow_candidate_window(tmp_path):
    """The display rule needs a window deeper than the display top-k."""
    from catalyst_eval.v1_1.recovery_retrieval import (
        CANDIDATE_DEPTH_FLOOR,
        RecoveryRetrievalError,
        default_retrieve,
    )

    conn = _conn(tmp_path, {"e1": "FULL_TEXT"})
    try:
        with pytest.raises(RecoveryRetrievalError, match="candidate_depth"):
            default_retrieve(
                conn,
                corpus_manifest_id=MANIFEST_ID,
                build_id=BUILD_ID,
                top_k=8,
                candidate_depth=CANDIDATE_DEPTH_FLOOR - 1,
            )
    finally:
        conn.close()
