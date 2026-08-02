"""Deterministic promoted-corpus lexical baseline contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from catalyst_eval.probes.lexical_baseline import (
    build_lexical_baseline_id,
    freeze_lexical_baseline,
    load_case_pack,
)


CASE_PACK = (
    Path(__file__).resolve().parents[1]
    / "golden_set"
    / "pre_b6_attribution_cases_v1.jsonl"
)


def test_case_pack_is_exactly_ordered_and_ratified():
    cases = load_case_pack(CASE_PACK)
    assert [case["case_id"] for case in cases] == [f"c{i:02d}" for i in range(1, 13)]
    assert len({case["ticker"] for case in cases}) == 12


def test_case_pack_rejects_duplicate_or_unknown_slot(tmp_path: Path):
    rows = [
        {"case_id": "c01", "slot": "single_source_answerable", "ticker": "AAPL"},
        {"case_id": "c01", "slot": "unknown", "ticker": "AAPL"},
    ]
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    with pytest.raises(ValueError):
        load_case_pack(path)


def test_baseline_identity_excludes_created_at_and_output_path():
    body = {
        "schema_version": "pre_b6_lexical_baseline_v1",
        "snapshot_id": "a" * 64,
        "corpus_manifest_id": "b" * 64,
        "cases": [],
    }
    first = build_lexical_baseline_id(body, created_at="2026-08-02T00:00:00Z", output_path="one")
    second = build_lexical_baseline_id(body, created_at="2026-08-03T00:00:00Z", output_path="two")
    assert first == second


def test_freeze_uses_certified_anchor_query_and_cutoff(tmp_path: Path, monkeypatch):
    import catalyst_eval.probes.lexical_baseline as baseline

    expected_tickers = {row[2] for row in baseline.EXPECTED_CASES}
    probe = {
        "probe_report_id": "c" * 64,
        "probe_cutoff": "2026-07-31T23:59:59Z",
        "lexical_results": [
            {
                "ticker": ticker,
                "ok": True,
                "mode_served": "fts5",
                "query_terms": ["frozen", ticker.lower()],
                "anchor_chunk_id": f"anchor-{ticker}",
                "anchor_document_id": f"document-{ticker}",
                "cutoff": "2025-12-31T00:00:00Z",
            }
            for ticker in sorted(expected_tickers)
        ],
    }
    probe["lexical_results"].extend(
        {
            "ticker": ticker,
            "ok": True,
            "mode_served": "fts5",
            "query_terms": ["certified", ticker.lower()],
            "anchor_chunk_id": f"anchor-{ticker}",
            "anchor_document_id": f"document-{ticker}",
            "cutoff": "2025-12-31T00:00:00Z",
        }
        for ticker in ("INTC", "QCOM")
    )
    monkeypatch.setattr(baseline, "load_and_verify_postbuild_readiness_report", lambda *a, **k: {
        "postbuild_readiness_id": "d" * 64,
    })
    monkeypatch.setattr(baseline, "load_and_verify_probe_report", lambda *a, **k: probe)
    monkeypatch.setattr(baseline, "verify_probe_report_against_db", lambda *a, **k: probe)

    class Conn:
        def execute(self, *_args):
            return SimpleNamespace(fetchone=lambda: (json.dumps({"tokenizer_revision": "r1"}),))

    captured = []

    def fake_retrieve(_conn, query, **kwargs):
        captured.append((query, kwargs))
        return SimpleNamespace(
            mode_served="fts5",
            is_degraded=False,
            results=(
                SimpleNamespace(
                    chunk_id="chunk-1",
                    document_id="doc-1",
                    lexical_rank=1,
                    lexical_raw_score=-1.0,
                    source_class="official_government",
                    available_at="2025-12-01T00:00:00Z",
                    corpus_manifest_id="b" * 64,
                    index_manifest_id=None,
                    mode_served="fts5",
                    is_degraded=False,
                    fallback_reason=None,
                ),
            ),
        )

    monkeypatch.setattr(baseline, "retrieve_lexical", fake_retrieve)
    baseline_id, body = freeze_lexical_baseline(
        Conn(),
        universe_manifest_id="a" * 64,
        snapshot_id="b" * 64,
        corpus_manifest_id="b" * 64,
        probe_report_path=tmp_path / "probe.json",
        postbuild_readiness_report_path=tmp_path / "postbuild.json",
        case_pack_path=CASE_PACK,
        output_path=tmp_path / "baseline.json",
    )
    assert baseline_id == body["baseline_id"]
    assert len(captured) == 12
    assert captured[0] == (
        "frozen aapl",
        {
            "ticker": "AAPL",
            "cutoff": "2025-12-31T00:00:00Z",
            "requested_manifest_id": "b" * 64,
            "candidate_depth": 20,
            "top_k": 20,
        },
    )
    assert body["look_ahead_count"] == 0
    assert body["look_ahead"] == 0
    assert all(case["look_ahead"] == 0 for case in body["cases"])


def test_freeze_rejects_future_result(tmp_path: Path, monkeypatch):
    import catalyst_eval.probes.lexical_baseline as baseline

    tickers = {row[2] for row in baseline.EXPECTED_CASES}
    probe = {
        "probe_report_id": "c" * 64,
        "probe_cutoff": "2026-07-31T23:59:59Z",
        "lexical_results": [
            {
                "ticker": ticker,
                "ok": True,
                "mode_served": "fts5",
                "query_terms": ["frozen"],
                "anchor_chunk_id": "anchor",
                "anchor_document_id": "document",
                "cutoff": "2025-12-31T00:00:00Z",
            }
            for ticker in tickers
        ],
    }
    monkeypatch.setattr(baseline, "load_and_verify_postbuild_readiness_report", lambda *a, **k: {"postbuild_readiness_id": "d" * 64})
    monkeypatch.setattr(baseline, "load_and_verify_probe_report", lambda *a, **k: probe)
    monkeypatch.setattr(baseline, "verify_probe_report_against_db", lambda *a, **k: probe)
    monkeypatch.setattr(baseline, "retrieve_lexical", lambda *_a, **_k: SimpleNamespace(
        mode_served="fts5",
        is_degraded=False,
        results=(SimpleNamespace(
            chunk_id="chunk", document_id="doc", lexical_rank=1,
            lexical_raw_score=-1.0, source_class="official_government",
            available_at="2026-01-01T00:00:00Z", corpus_manifest_id="b" * 64,
            index_manifest_id=None, mode_served="fts5", is_degraded=False,
            fallback_reason=None,
        ),),
    ))

    class Conn:
        def execute(self, *_args):
            return SimpleNamespace(fetchone=lambda: (json.dumps({}),))

    with pytest.raises(ValueError, match="look-ahead"):
        freeze_lexical_baseline(
            Conn(), universe_manifest_id="a" * 64, snapshot_id="b" * 64,
            corpus_manifest_id="b" * 64, probe_report_path=tmp_path / "probe",
            postbuild_readiness_report_path=tmp_path / "postbuild",
            case_pack_path=CASE_PACK, output_path=tmp_path / "baseline.json",
        )
