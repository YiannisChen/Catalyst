import importlib.util
import json
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "p1_ablation.py"


def load_script_module(script_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _write_golden(path: Path) -> None:
    rows = [
        {"id": "c1", "ticker": "NVDA", "trade_date": "2025-10-28", "expected_status": "SUPPORTED"},
        {"id": "c2", "ticker": "NVDA", "trade_date": "2025-10-28", "expected_status": "SUPPORTED"},
        {"id": "c3", "ticker": "NVDA", "trade_date": "2025-10-28", "expected_status": "SUPPORTED"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _summary_payload(case_id: str) -> dict:
    return {
        "case": {"id": case_id, "expected_status": "SUPPORTED", "should_refuse": False},
        "judge": {"output_status": "SUPPORTED"},
        "metrics": {
            "attribution_f1": 1.0,
            "category_accuracy": 1.0,
            "grounding_rate": 1.0,
            "temporal_precision": 1.0,
            "confidence_calibration": 1.0,
        },
        "cost_and_trace": {"total_latency_ms": 100, "total_tokens": 10, "total_cost_usd": 0.01},
        "retrieval": {
            "retrieved_counts": {"total": 1, "l1": 1, "l2": 0},
            "reranked_counts": {"total": 1, "l1": 1, "l2": 0},
        },
        "critic": {"magnitude_coverage": 1.0},
        "citations": {"citation_precision": 1.0},
    }


def test_ablation_default_mode_stops_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ab = load_script_module(str(SCRIPT_PATH), "p1_ablation_resilience_default")
    golden = tmp_path / "golden.jsonl"
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_golden(golden)

    calls: list[str] = []

    def fake_run_one(*, case_id: str, out_dir: Path, **kwargs):
        calls.append(case_id)
        if case_id == "c2":
            raise RuntimeError("boom-c2")
        summary_path = out_dir / f"ok_{case_id}.json"
        summary_path.write_text(json.dumps(_summary_payload(case_id)), encoding="utf-8")
        return summary_path

    monkeypatch.setattr(ab, "_run_one", fake_run_one)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "p1_ablation.py",
            "--golden-set",
            str(golden),
            "--out-dir",
            str(out_dir),
            "--profiles",
            "full",
            "--case-ids",
            "c1,c2,c3",
            "--tag",
            "res_default",
        ],
    )

    with pytest.raises(RuntimeError, match="boom-c2"):
        ab.main()
    assert calls == ["c1", "c2"]


def test_ablation_continue_on_error_keeps_running_and_records_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ab = load_script_module(str(SCRIPT_PATH), "p1_ablation_resilience_continue")
    golden = tmp_path / "golden.jsonl"
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_golden(golden)

    calls: list[str] = []

    def fake_run_one(*, case_id: str, out_dir: Path, **kwargs):
        calls.append(case_id)
        if case_id == "c2":
            raise RuntimeError("boom-c2")
        summary_path = out_dir / f"ok_{case_id}.json"
        summary_path.write_text(json.dumps(_summary_payload(case_id)), encoding="utf-8")
        return summary_path

    monkeypatch.setattr(ab, "_run_one", fake_run_one)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "p1_ablation.py",
            "--golden-set",
            str(golden),
            "--out-dir",
            str(out_dir),
            "--profiles",
            "full",
            "--case-ids",
            "c1,c2,c3",
            "--tag",
            "res_continue",
            "--continue-on-error",
        ],
    )

    rc = ab.main()
    assert rc == 0
    assert calls == ["c1", "c2", "c3"]

    payload = json.loads((out_dir / "res_continue_p1_ablation.json").read_text(encoding="utf-8"))
    assert payload["run_status"] == "completed_with_errors"
    assert payload["num_failed_cases"] == 1
    assert len(payload["failed_cases"]) == 1
    failed = payload["failed_cases"][0]
    assert failed["index_variant"] == "l1l2"
    assert failed["profile"] == "full"
    assert failed["case_id"] == "c2"
    assert failed["error_type"] == "RuntimeError"
    assert "boom-c2" in failed["error_message"]
    assert isinstance(failed["command"], list)
