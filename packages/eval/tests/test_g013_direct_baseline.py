import importlib.util
import json
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "g013_direct_baseline.py"


def load_script_module(script_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _ok_result(model_id: str) -> dict:
    return {
        "model_id": model_id,
        "answer_text": f"{model_id}: grounded answer.",
        "latency_ms": 123,
        "total_tokens": 321,
        "total_cost_usd": 0.0123,
        "status_class": "SUFFICIENT",
        "hallucination_flag": False,
        "error_type": None,
        "error_message": None,
    }


def test_continue_on_error_records_failures_and_keeps_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mod = load_script_module(str(SCRIPT_PATH), "g013_direct_baseline_continue")
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    calls: list[str] = []

    def fake_invoke(*, model_id: str, query: str, provider: str, base_url: str, temperature: float, max_output_tokens: int, llm_seed: int | None):
        calls.append(model_id)
        if model_id == "deepseek-v4-flash":
            raise RuntimeError("model timeout")
        return _ok_result(model_id)

    monkeypatch.setattr(mod, "_invoke_direct_model", fake_invoke)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "g013_direct_baseline.py",
            "--models",
            "gemini-2.5-flash-nothink,deepseek-v4-flash,qwen3.6-flash",
            "--continue-on-error",
            "--out-dir",
            str(out_dir),
            "--tag",
            "ut_direct",
        ],
    )

    rc = mod.main()
    assert rc == 0
    assert calls == ["gemini-2.5-flash-nothink", "deepseek-v4-flash", "qwen3.6-flash"]

    payload = json.loads((out_dir / "ut_direct_g013_direct_baseline.json").read_text(encoding="utf-8"))
    assert payload["run_status"] == "completed_with_errors"
    assert payload["tag"] == "ut_direct"
    assert payload["provider"] == "aihubmix"
    assert payload["base_url"] == "https://aihubmix.com/v1"

    assert len(payload["per_model"]) == 3
    assert len(payload["failed_models"]) == 1
    failed = payload["failed_models"][0]
    assert failed["model_id"] == "deepseek-v4-flash"
    assert failed["error_type"] == "RuntimeError"
    assert "timeout" in failed["error_message"]

    for row in payload["per_model"]:
        assert "model_id" in row
        assert "answer_text" in row
        assert "latency_ms" in row
        assert "total_tokens" in row
        assert "total_cost_usd" in row
        assert "status_class" in row
        assert "hallucination_flag" in row
        assert "error_type" in row
        assert "error_message" in row


def test_without_continue_on_error_stops_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mod = load_script_module(str(SCRIPT_PATH), "g013_direct_baseline_stop")
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    calls: list[str] = []

    def fake_invoke(*, model_id: str, query: str, provider: str, base_url: str, temperature: float, max_output_tokens: int, llm_seed: int | None):
        calls.append(model_id)
        if model_id == "deepseek-v4-flash":
            raise RuntimeError("boom")
        return _ok_result(model_id)

    monkeypatch.setattr(mod, "_invoke_direct_model", fake_invoke)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "g013_direct_baseline.py",
            "--models",
            "gemini-2.5-flash-nothink,deepseek-v4-flash,qwen3.6-flash",
            "--out-dir",
            str(out_dir),
            "--tag",
            "ut_stop",
        ],
    )

    with pytest.raises(RuntimeError, match="boom"):
        mod.main()
    assert calls == ["gemini-2.5-flash-nothink", "deepseek-v4-flash"]
