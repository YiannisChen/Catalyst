from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT_PATH = Path(__file__).resolve().parents[3] / "packages" / "eval" / "scripts" / "run_frozen_eval.py"
SPEC = importlib.util.spec_from_file_location("run_frozen_eval_script", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_resolve_pipeline_runtime_sql_only_keeps_sql_path(tmp_path: Path):
    table, embedding_fn, effective = MODULE._resolve_pipeline_retrieval_runtime(
        MODULE.PIPELINE_MODE_SQL_ONLY,
        tmp_path / "missing",
    )

    assert table is None
    assert embedding_fn is None
    assert effective == MODULE.PIPELINE_MODE_SQL_ONLY


def test_parse_args_defaults_to_sql_only(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_frozen_eval.py"])
    args = MODULE._parse_args()
    assert args.pipeline_mode == MODULE.PIPELINE_MODE_SQL_ONLY


def test_resolve_pipeline_runtime_rag_only_uses_lancedb_when_available(tmp_path: Path, monkeypatch):
    fake_table = object()
    monkeypatch.setattr(MODULE, "_open_lancedb_table", lambda _p: fake_table)

    table, embedding_fn, effective = MODULE._resolve_pipeline_retrieval_runtime(
        MODULE.PIPELINE_MODE_RAG_ONLY,
        tmp_path,
    )

    assert table is fake_table
    assert callable(embedding_fn)
    assert len(embedding_fn("why did AAPL move?")) == MODULE.RAG_ONLY_EMBEDDING_DIM
    assert effective == MODULE.PIPELINE_MODE_RAG_ONLY


def test_resolve_pipeline_runtime_rag_only_falls_back_to_sql_when_lancedb_unavailable(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(MODULE, "_open_lancedb_table", lambda _p: None)

    table, embedding_fn, effective = MODULE._resolve_pipeline_retrieval_runtime(
        MODULE.PIPELINE_MODE_RAG_ONLY,
        tmp_path / "missing",
    )

    assert table is None
    assert embedding_fn is None
    assert effective == MODULE.PIPELINE_MODE_SQL_ONLY


def test_run_mcj_cases_binds_table_and_embedding_fn_for_rag_only(tmp_path: Path, monkeypatch):
    captured: dict[str, object] = {}

    class FakeGraph:
        def invoke(self, state):
            return {
                **state,
                "run_id": "run-1",
                "trace_id": "trace-1",
                "output_status": "SUFFICIENT",
                "causes": [],
                "summary_md": "ok",
                "retrieved_chunks": [
                    {
                        "asset_id": "a1",
                        "content_md": "content",
                        "source_type": "polygon_news",
                        "rrf_score": 0.2,
                    }
                ],
                "reranked_chunks": [
                    {
                        "asset_id": "a1",
                        "content_md": "content",
                        "source_type": "polygon_news",
                        "rrf_score": 0.2,
                    }
                ],
                "graded_evidence": [],
                "critic_decision": None,
                "validation_error": None,
                "cost_breakdown": [],
                "total_cost_usd": 0.0,
                "total_tokens": 0,
            }

    def fake_build_attribution_graph(**kwargs):
        captured.update(kwargs)
        return FakeGraph()

    monkeypatch.setattr(MODULE, "build_attribution_graph", fake_build_attribution_graph)
    monkeypatch.setattr(
        MODULE,
        "export_run",
        lambda run_id, out_path, db_path: {
            "run_id": run_id,
            "trace_id": "trace-1",
            "total_latency_ms": 0,
        },
    )

    table = object()
    embedding_fn = lambda text: [0.0] * MODULE.RAG_ONLY_EMBEDDING_DIM
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()

    cases = [
        {
            "id": "g001",
            "ticker": "AAPL",
            "trade_date": "2026-01-15",
            "price_move_pct": -1.2,
            "expected_status": "SUFFICIENT",
            "should_refuse": False,
        }
    ]
    fixtures = {
        ("AAPL", "2026-01-15"): {
            "id": "g001",
            "ticker": "AAPL",
            "trade_date": "2026-01-15",
            "price_move_pct": -1.2,
            "golden_causes": [],
            "expected_status": "SUFFICIENT",
            "should_refuse": False,
            "direct_evidence_ids": ["a1"],
        }
    }

    rows, _raw_results, _observations = MODULE._run_mcj_cases(
        cases,
        fixtures=fixtures,
        frozen_db=tmp_path / "frozen.db",
        trace_db=tmp_path / "trace.db",
        trace_dir=tmp_path / "traces",
        lancedb_dir=lancedb_dir,
        table=table,
        embedding_fn=embedding_fn,
    )

    assert captured["use_critic"] is True
    assert captured["table"] is table
    assert captured["embedding_fn"] is embedding_fn
    assert rows[0]["retrieved_count"] == 1
    assert rows[0]["reranked_count"] == 1
