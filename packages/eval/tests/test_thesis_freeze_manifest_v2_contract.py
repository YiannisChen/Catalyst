from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "build_thesis_freeze_manifest.py"
    spec = importlib.util.spec_from_file_location("build_thesis_freeze_manifest", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_manifest_v2_has_audit_fields(tmp_path: Path):
    mod = _load_module()

    ablation_rows = []
    golden_lines = []
    for i in range(1, 51):
        cid = f"g{i:03d}"
        for profile in ("full", "no_vector"):
            if profile == "full" and cid == "g012":
                continue
            ablation_rows.append(
                {
                    "case_id": cid,
                    "profile": profile,
                    "expected_status": "SUFFICIENT",
                    "output_status": "SUFFICIENT",
                }
            )
        golden_lines.append(
            json.dumps(
                {
                    "id": cid,
                    "ticker": "AAPL",
                    "trade_date": "2026-01-15",
                    "should_refuse": False,
                    "query_override": f"why {cid}",
                }
            )
        )

    ablation = {
        "per_case": ablation_rows,
        "failed_cases": [{"case_id": "g012", "profile": "full"}],
    }

    ablation_path = tmp_path / "ablation.json"
    ablation_path.write_text(json.dumps(ablation), encoding="utf-8")
    golden_path = tmp_path / "golden.jsonl"
    golden_path.write_text("\n".join(golden_lines) + "\n", encoding="utf-8")
    db_path = tmp_path / "db.sqlite"
    db_path.write_bytes(b"db")
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    (lancedb_dir / "chunks.lance").write_bytes(b"lance")
    model_lock_path = tmp_path / "model.lock.json"
    model_lock_path.write_text("{}", encoding="utf-8")

    payload = mod.build_manifest(
        main_ablation_json=ablation_path,
        golden_set=golden_path,
        db_path=db_path,
        lancedb_dir=lancedb_dir,
        model_lock_path=model_lock_path,
    )

    assert payload["effective_n"] == 99
    assert "failed_case_ids" in payload
    assert "input_sha256" in payload
    assert "prompt_contract" in payload
    assert payload["prompt_contract"]["direct_prompt_has_expected_status"] is False
