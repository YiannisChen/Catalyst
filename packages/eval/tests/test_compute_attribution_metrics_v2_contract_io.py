from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "compute_attribution_metrics_v2.py"
    spec = importlib.util.spec_from_file_location("compute_attribution_metrics_v2", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_attribution_v2_maps_from_real_case_schema(tmp_path: Path):
    mod = _load_module()
    catalyst_rows = [
        {
            "case_id": "g001",
            "profile": "full",
            "causes": [{"category": "macro", "text": "tariff pause boosted risk appetite"}],
            "gold_categories": ["macro"],
            "gold_causes": ["risk-on rally after tariff pause"],
        }
    ]
    direct_rows = [
        {
            "case_id": "g001",
            "profile": "full",
            "answer": "Risk-on after tariff pause.",
            "pred_categories": ["macro"],
            "gold_categories": ["macro"],
            "gold_causes": ["risk-on rally after tariff pause"],
        }
    ]
    out = mod.compute_metrics(catalyst_rows=catalyst_rows, direct_rows=direct_rows)
    assert out["catalyst"]["category_f1"] >= 0.0
    assert out["direct_llm"]["cause_semantic_sim"] >= 0.0


def test_attribution_v2_fails_fast_when_required_fields_missing(tmp_path: Path):
    mod = _load_module()
    with pytest.raises(ValueError, match="missing required attribution fields"):
        mod.compute_metrics(catalyst_rows=[{"case_id": "g001"}], direct_rows=[{"case_id": "g001"}])


def test_cli_strict_mode_fails_without_required_fields(tmp_path: Path):
    script = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "compute_attribution_metrics_v2.py"
    c = tmp_path / "c.jsonl"
    d = tmp_path / "d.jsonl"
    out = tmp_path / "o.json"
    c.write_text(json.dumps({"case_id": "g001"}) + "\n", encoding="utf-8")
    d.write_text(json.dumps({"case_id": "g001"}) + "\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script), "--catalyst-json", str(c), "--direct-json", str(d), "--output-json", str(out)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "real_schema mode failed" in (proc.stdout + proc.stderr)


def test_cli_allows_legacy_fallback_only_when_flag_set(tmp_path: Path):
    script = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "compute_attribution_metrics_v2.py"
    c = tmp_path / "c.jsonl"
    d = tmp_path / "d.jsonl"
    out = tmp_path / "o.json"
    c.write_text(json.dumps({"pred_categories": ["macro"], "gold_categories": ["macro"], "pred_causes": ["x"], "gold_causes": ["x"]}) + "\n", encoding="utf-8")
    d.write_text(json.dumps({"pred_categories": ["macro"], "gold_categories": ["macro"], "pred_causes": ["x"], "gold_causes": ["x"]}) + "\n", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--catalyst-json",
            str(c),
            "--direct-json",
            str(d),
            "--output-json",
            str(out),
            "--allow-legacy-fallback",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["compute_mode"] in {"real_schema", "legacy_fallback"}
