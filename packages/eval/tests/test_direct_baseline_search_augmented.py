from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "run_direct_llm_frozen_baseline.py"
    spec = importlib.util.spec_from_file_location("run_direct_llm_frozen_baseline", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_search_mode_injects_topk_context():
    mod = _load_module()
    unit = {"case_id": "g001", "profile": "full", "ticker": "TSLA", "trade_date": "2025-01-02", "query": "why"}
    rows = [
        {
            "case_id": "g001",
            "profile": "full",
            "search_candidates": [
                {"chunk_id": "c1", "source_rank": 0, "content": "TSLA deliveries miss"},
                {"chunk_id": "c2", "source_rank": 1, "content": "macro risk off"},
            ],
        }
    ]
    prompt, meta = mod._build_prompt_with_context(unit, baseline_mode="search_augmented", search_rows=rows)
    assert "Search Evidence" in prompt
    assert meta["search_hits_count"] > 0


def test_search_mode_prefers_content_md_and_fallbacks_to_content():
    mod = _load_module()
    unit = {"case_id": "g001", "profile": "full", "ticker": "TSLA", "trade_date": "2025-01-02", "query": "why"}
    rows = [
        {
            "case_id": "g001",
            "profile": "full",
            "search_candidates": [
                {
                    "chunk_id": "c_md_first",
                    "source_rank": 0,
                    "content_md": "REAL_MD_TEXT",
                    "content": "LEGACY_CONTENT_TEXT",
                },
                {
                    "chunk_id": "c_fallback",
                    "source_rank": 1,
                    "content": "ONLY_LEGACY_CONTENT",
                },
            ],
        }
    ]
    prompt, _ = mod._build_prompt_with_context(unit, baseline_mode="search_augmented", search_rows=rows)
    assert "REAL_MD_TEXT" in prompt
    assert "ONLY_LEGACY_CONTENT" in prompt
    assert "LEGACY_CONTENT_TEXT" not in prompt
