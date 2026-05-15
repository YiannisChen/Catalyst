from pathlib import Path
import json
import sqlite3

import importlib.util

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = PROJECT_ROOT / "scripts/reports/prepare_r1_external_chunks.py"
SPEC = importlib.util.spec_from_file_location("prepare_r1_external_chunks", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
build_rows = MODULE.build_rows



def test_build_rows_extracts_20_cases_with_chunk_text(tmp_path):
    summary_path = tmp_path / "g001.summary.json"
    summary_path.write_text(json.dumps({
        "case": {"id": "g001", "ticker": "TSLA", "trade_date": "2025-01-02", "price_move_pct": -6.08},
        "critic": {"all_graded_scores": [{"chunk_id": "asset-1::l2s0001", "relevance": 0.8, "category": "macro"}]},
        "run": {"tag": "t"}
    }), encoding="utf-8")

    db_path = tmp_path / "mock.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE clean_assets (asset_id TEXT, content_md TEXT)")
    conn.execute("INSERT INTO clean_assets VALUES (?, ?)", ("asset-1", "Sentence A. Sentence B. Sentence C."))
    conn.commit()
    conn.close()

    rows = build_rows(summary_paths=[summary_path], db_path=db_path, case_ids={"g001"})
    assert len(rows) == 1
    assert rows[0]["case_id"] == "g001"
    assert rows[0]["chunks"][0]["chunk_text"]
