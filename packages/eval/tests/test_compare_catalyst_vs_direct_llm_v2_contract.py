from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "compare_catalyst_vs_direct_llm.py"
    spec = importlib.util.spec_from_file_location("compare_catalyst_vs_direct_llm", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_comparison_enforces_identical_case_profile_universe(tmp_path: Path):
    mod = _load_module()
    freeze = {
        "frozen_units": [{"case_id": "g001", "profile": "full", "expected_status": "SUFFICIENT", "should_refuse": False}],
    }
    catalyst = {"per_case": []}
    direct_rows = []

    freeze_p = _write_json(tmp_path / "freeze.json", freeze)
    catalyst_p = _write_json(tmp_path / "catalyst.json", catalyst)
    direct_p = _write_jsonl(tmp_path / "direct.jsonl", direct_rows)

    with pytest.raises(subprocess.CalledProcessError):
        subprocess.check_call([
            sys.executable,
            str(Path(__file__).resolve().parents[3] / "scripts" / "reports" / "compare_catalyst_vs_direct_llm.py"),
            "--freeze-manifest", str(freeze_p),
            "--catalyst-ablation-json", str(catalyst_p),
            "--direct-per-unit-jsonl", str(direct_p),
            "--output-json", str(tmp_path / "out.json"),
            "--output-csv", str(tmp_path / "out.csv"),
            "--output-md", str(tmp_path / "out.md"),
        ])


def test_comparison_emits_error_taxonomy_and_audit_breakdown(tmp_path: Path):
    mod = _load_module()
    freeze = {
        "frozen_units": [{"case_id": "g001", "profile": "full", "expected_status": "SUFFICIENT", "should_refuse": False}],
        "freeze_id": "f1",
        "main_run_tag": "r1",
    }
    catalyst = {"per_case": [{"case_id": "g001", "profile": "full", "expected_status": "SUFFICIENT", "output_status": "SUFFICIENT", "grounding_rate": 1.0}]}
    direct_rows = [{"case_id": "g001", "profile": "full", "expected_status": "SUFFICIENT", "should_refuse": False, "output_status": "SUFFICIENT", "refusal_flag": False, "latency_ms": 1.0, "total_tokens": 10, "total_cost_usd": 0.1}]

    freeze_p = _write_json(tmp_path / "freeze.json", freeze)
    catalyst_p = _write_json(tmp_path / "catalyst.json", catalyst)
    direct_p = _write_jsonl(tmp_path / "direct.jsonl", direct_rows)

    subprocess.check_call([
        sys.executable,
        str(Path(__file__).resolve().parents[3] / "scripts" / "reports" / "compare_catalyst_vs_direct_llm.py"),
        "--freeze-manifest", str(freeze_p),
        "--catalyst-ablation-json", str(catalyst_p),
        "--direct-per-unit-jsonl", str(direct_p),
        "--output-json", str(tmp_path / "out.json"),
        "--output-csv", str(tmp_path / "out.csv"),
        "--output-md", str(tmp_path / "out.md"),
    ])
    out = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert "error_taxonomy" in out
    assert "profile_breakdown" in out
    assert "fairness_notes" in out
