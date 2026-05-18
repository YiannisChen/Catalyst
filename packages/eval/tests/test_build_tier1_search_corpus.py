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


def test_tier1_candidates_stable_order(tmp_path: Path):
    mod = _load_module()
    r1 = mod.build_tier1_search_corpus(_manifest(tmp_path))
    r2 = mod.build_tier1_search_corpus(_manifest(tmp_path))
    assert r1 == r2
