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


def _manifest(tmp_path: Path) -> Path:
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({
        "frozen_units": [
            {"case_id": "g001", "profile": "full", "evidence_chunks": [{"chunk_id": "c1", "content_md": "abc"}]},
            {"case_id": "g002", "profile": "full", "evidence_chunks": []},
        ]
    }))
    return p


def test_build_same_evidence_inputs_emits_case_profile_aligned_rows(tmp_path: Path):
    mod = _load_module()
    rows, meta = mod.build_same_evidence_inputs(_manifest(tmp_path))
    assert rows[0]["case_id"] == "g001"
    assert rows[0]["profile"] == "full"
    assert len(rows[0]["evidence_chunks"]) > 0
    assert rows[0]["evidence_chunks_count"] > 0


def test_same_evidence_only_keeps_evidence_nonempty_cases(tmp_path: Path):
    mod = _load_module()
    rows, meta = mod.build_same_evidence_inputs(_manifest(tmp_path))
    assert all(r["evidence_chunks_count"] > 0 for r in rows)
    assert meta["tier2_eligible_n"] == 1
    assert meta["tier2_excluded_n"] == 1
    assert meta["tier2_exclusion_reason_counts"]["no_evidence_chunks"] == 1
