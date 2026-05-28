from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "build_same_evidence_inputs.py"
    spec = importlib.util.spec_from_file_location("build_same_evidence_inputs", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _manifest_and_summaries(tmp_path: Path) -> Path:
    s1 = tmp_path / "data" / "eval_reports" / "g001.summary.json"
    s1.parent.mkdir(parents=True, exist_ok=True)
    s1.write_text(json.dumps({
        "retrieval": {
            "reranked_chunks": [
                {"chunk_id": "rk1", "content_md": "TSLA deliveries miss", "source": "polygon_news"}
            ]
        }
    }), encoding="utf-8")

    s2 = tmp_path / "data" / "eval_reports" / "g002.summary.json"
    s2.write_text(json.dumps({"retrieval": {"reranked_chunks": []}}), encoding="utf-8")

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "frozen_units": [
            {"case_id": "g001", "profile": "full", "summary_json": "/root/Catalyst/data/eval_reports/g001.summary.json"},
            {"case_id": "g002", "profile": "full", "summary_json": "/root/Catalyst/data/eval_reports/g002.summary.json"},
        ]
    }), encoding="utf-8")
    return manifest


def test_build_same_evidence_reads_summary_paths_and_extracts_reranked_chunks(tmp_path: Path):
    mod = _load_module()
    manifest = _manifest_and_summaries(tmp_path)
    rows, meta = mod.build_same_evidence_inputs(manifest_path=manifest, repo_root=tmp_path)
    assert rows[0]["case_id"] == "g001"
    assert rows[0]["evidence_chunks_count"] > 0
    assert meta["tier2_eligible_n"] > 0


def test_build_same_evidence_excludes_cases_with_no_reranked_chunks(tmp_path: Path):
    mod = _load_module()
    manifest = _manifest_and_summaries(tmp_path)
    rows, meta = mod.build_same_evidence_inputs(manifest_path=manifest, repo_root=tmp_path)
    assert meta["tier2_excluded_n"] == 1
    assert meta["tier2_exclusion_reason_counts"]["no_reranked_chunks"] == 1
