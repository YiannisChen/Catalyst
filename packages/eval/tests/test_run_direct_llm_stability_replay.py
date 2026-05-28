from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "run_direct_llm_stability_replay.py"
    spec = importlib.util.spec_from_file_location("run_direct_llm_stability_replay", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_replay_outputs_consistency_metrics():
    mod = _load_module()
    runs = [
        [{"case_id": "g001", "profile": "full", "output_status": "SUFFICIENT", "refusal_flag": False}],
        [{"case_id": "g001", "profile": "full", "output_status": "SUFFICIENT", "refusal_flag": False}],
        [{"case_id": "g001", "profile": "full", "output_status": "PARTIAL", "refusal_flag": False}],
    ]
    out = mod.aggregate_consistency(runs)
    assert "status_consistency_rate" in out
    assert "refusal_consistency_rate" in out
    assert "per_case_transition_counts" in out


def test_cli_freeze_mode_requires_real_run_inputs(tmp_path: Path):
    freeze = {
        "frozen_units": [
            {"case_id": "g001", "profile": "full", "expected_status": "SUFFICIENT", "should_refuse": False}
        ]
    }
    freeze_path = tmp_path / "freeze.json"
    freeze_path.write_text(json.dumps(freeze), encoding="utf-8")
    out_path = tmp_path / "stability.json"
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.check_call(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[3] / "scripts" / "reports" / "run_direct_llm_stability_replay.py"),
                "--freeze-manifest",
                str(freeze_path),
                "--model",
                "deepseek-v4-flash",
                "--provider",
                "aihubmix",
                "--base-url",
                "https://aihubmix.com/v1",
                "--k-runs",
                "3",
                "--out-json",
                str(out_path),
            ]
        )


def test_cli_freeze_mode_accepts_real_jsonl_runs(tmp_path: Path):
    freeze = {
        "frozen_units": [
            {"case_id": "g001", "profile": "full", "expected_status": "SUFFICIENT", "should_refuse": False}
        ]
    }
    freeze_path = tmp_path / "freeze.json"
    freeze_path.write_text(json.dumps(freeze), encoding="utf-8")
    run1 = tmp_path / "run1.jsonl"
    run2 = tmp_path / "run2.jsonl"
    run3 = tmp_path / "run3.jsonl"
    run1.write_text(json.dumps({"case_id": "g001", "profile": "full", "output_status": "SUFFICIENT", "refusal_flag": False}) + "\n", encoding="utf-8")
    run2.write_text(json.dumps({"case_id": "g001", "profile": "full", "output_status": "SUFFICIENT", "refusal_flag": False}) + "\n", encoding="utf-8")
    run3.write_text(json.dumps({"case_id": "g001", "profile": "full", "output_status": "PARTIAL", "refusal_flag": False}) + "\n", encoding="utf-8")
    out_path = tmp_path / "stability.json"
    subprocess.check_call(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[3] / "scripts" / "reports" / "run_direct_llm_stability_replay.py"),
            "--freeze-manifest",
            str(freeze_path),
            "--model",
            "deepseek-v4-flash",
            "--provider",
            "aihubmix",
            "--base-url",
            "https://aihubmix.com/v1",
            "--k-runs",
            "3",
            "--run-jsonl",
            str(run1),
            "--run-jsonl",
            str(run2),
            "--run-jsonl",
            str(run3),
            "--out-json",
            str(out_path),
        ]
    )
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["k_runs"] == 3
    assert payload["status_consistency_rate"] < 1.0
    assert len(payload["run_sources"]) == 3
