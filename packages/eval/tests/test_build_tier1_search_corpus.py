from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "build_tier1_search_corpus.py"
    spec = importlib.util.spec_from_file_location("build_tier1_search_corpus", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _manifest(tmp_path: Path) -> Path:
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({
        "effective_n": 2,
        "frozen_units": [
            {"case_id": "g002", "profile": "no_vector", "ticker": "TSLA", "trade_date": "2025-01-03", "query": "q2"},
            {"case_id": "g001", "profile": "full", "ticker": "AAPL", "trade_date": "2025-01-02", "query": "q1"},
        ],
    }))
    return p


def test_tier1_corpus_has_required_fields(tmp_path: Path):
    mod = _load_module()
    rows = mod.build_tier1_search_corpus(_manifest(tmp_path))
    assert rows
    assert {"case_id", "profile", "ticker", "trade_date", "query", "search_candidates"} <= set(rows[0].keys())


def test_tier1_corpus_covers_all_frozen_units(tmp_path: Path):
    mod = _load_module()
    rows = mod.build_tier1_search_corpus(_manifest(tmp_path))
    assert len(rows) == 2
    assert [(r["case_id"], r["profile"]) for r in rows] == [("g001", "full"), ("g002", "no_vector")]


def test_tier1_corpus_uses_real_sources_not_synthetic_placeholder(tmp_path: Path):
    mod = _load_module()
    rows = mod.build_tier1_search_corpus(_manifest(tmp_path))
    cand0 = rows[0]["search_candidates"][0]
    assert cand0["source"] in {"polygon_news", "fmp_fundamentals", "lancedb"}
    assert "cand0" not in cand0["chunk_id"]
    assert "News evidence for" not in cand0["content_md"]


def test_tier1_candidates_stable_order(tmp_path: Path):
    mod = _load_module()
    r1 = mod.build_tier1_search_corpus(_manifest(tmp_path))
    r2 = mod.build_tier1_search_corpus(_manifest(tmp_path))
    assert r1 == r2


def test_tier1_corpus_can_use_explicit_repo_root_and_avoid_seed_when_real_summary_exists(tmp_path: Path):
    mod = _load_module()
    repo_root = tmp_path / "repo"
    (repo_root / "data" / "eval_reports").mkdir(parents=True)
    (repo_root / "scripts" / "reports").mkdir(parents=True)
    summary = {
        "retrieval": {
            "reranked_chunks": [
                {
                    "chunk_id": "real:polygon:1",
                    "source": "polygon_news",
                    "content_md": "TSLA moved after deliveries miss and tariff concerns on 2025-01-03.",
                }
            ]
        }
    }
    (repo_root / "data" / "eval_reports" / "x_p1_trace.summary.json").write_text(json.dumps(summary), encoding="utf-8")
    rows = mod.build_tier1_search_corpus(_manifest(tmp_path), repo_root=repo_root)
    assert rows and rows[0]["search_candidates"]
    assert all(not c["chunk_id"].startswith("seed:") for r in rows for c in r["search_candidates"])
