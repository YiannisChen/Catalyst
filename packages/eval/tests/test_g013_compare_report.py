import importlib.util
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "g013_compare_report.py"


def load_script_module(script_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_compare_report_handles_missing_side_and_writes_markdown(tmp_path: Path, monkeypatch):
    mod = load_script_module(str(SCRIPT_PATH), "g013_compare_report_test")

    direct_payload = {
        "tag": "exp1",
        "generated_at_utc": "2026-05-10T00:00:00Z",
        "provider": "aihubmix",
        "base_url": "https://aihubmix.com/v1",
        "query": "Why did NVDA move?",
        "per_model": [
            {
                "model_id": "gemini-2.5-flash-nothink",
                "status_class": "SUFFICIENT",
                "hallucination_flag": False,
                "latency_ms": 1000,
                "total_cost_usd": 0.02,
            },
            {
                "model_id": "qwen-turbo",
                "status_class": "SUFFICIENT",
                "hallucination_flag": True,
                "latency_ms": 900,
                "total_cost_usd": 0.01,
            },
        ],
        "failed_models": [],
        "run_status": "completed",
    }

    direct_json = tmp_path / "direct.json"
    direct_json.write_text(json.dumps(direct_payload), encoding="utf-8")

    catalyst_dir = tmp_path / "catalyst"
    catalyst_dir.mkdir(parents=True, exist_ok=True)
    catalyst_summary = {
        "run": {"model": "gemini-2.5-flash-nothink", "run_tag": "exp1", "provider": "aihubmix"},
        "judge": {"output_status": "SUFFICIENT"},
        "metrics": {"grounding_rate": 0.91},
        "citations": {"citation_precision": 0.87},
        "cost_and_trace": {"total_latency_ms": 1800, "total_cost_usd": 0.03},
    }
    (catalyst_dir / "exp1_g013_p1_trace.summary.json").write_text(
        json.dumps(catalyst_summary), encoding="utf-8"
    )

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "g013_compare_report.py",
            "--direct-json",
            str(direct_json),
            "--catalyst-input",
            str(catalyst_dir),
            "--out-dir",
            str(out_dir),
            "--tag",
            "exp1",
        ],
    )

    rc = mod.main()
    assert rc == 0

    out_json = out_dir / "exp1_g013_dual_track_compare.json"
    out_md = out_dir / "exp1_g013_dual_track_compare.md"
    assert out_json.exists()
    assert out_md.exists()

    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert "rows" in payload
    assert len(payload["rows"]) == 2
    assert payload["comparison_mode"] == "single_catalyst_vs_multi_direct"

    by_model = {row["model_id"]: row for row in payload["rows"]}
    assert by_model["gemini-2.5-flash-nothink"]["verdict"]["catalyst_better_on_reliability"] in {True, False}
    assert isinstance(by_model["gemini-2.5-flash-nothink"]["verdict"]["catalyst_better_on_reliability"], bool)

    # single-catalyst mode: qwen-turbo should reuse that single catalyst baseline
    qwen_row = by_model["qwen-turbo"]
    assert qwen_row["catalyst"]["missing"] is False
    assert isinstance(qwen_row["verdict"]["catalyst_better_on_reliability"], bool)

    md_text = out_md.read_text(encoding="utf-8")
    assert "comparison_mode" in md_text
    assert "single_catalyst_vs_multi_direct" in md_text
    assert "| model_id |" in md_text
    assert "gemini-2.5-flash-nothink" in md_text


def test_compare_report_default_filenames_when_tag_missing(tmp_path: Path, monkeypatch):
    mod = load_script_module(str(SCRIPT_PATH), "g013_compare_report_default_name")

    direct_payload = {
        "tag": "exp0",
        "generated_at_utc": "2026-05-10T00:00:00Z",
        "provider": "aihubmix",
        "base_url": "https://aihubmix.com/v1",
        "query": "Why did NVDA move?",
        "per_model": [
            {
                "model_id": "gemini-2.5-flash-nothink",
                "status_class": "SUFFICIENT",
                "hallucination_flag": False,
                "latency_ms": 1000,
                "total_cost_usd": 0.02,
            }
        ],
        "failed_models": [],
        "run_status": "completed",
    }
    direct_json = tmp_path / "direct.json"
    direct_json.write_text(json.dumps(direct_payload), encoding="utf-8")

    catalyst_summary = {
        "run": {"model": "gemini-2.5-flash-nothink", "run_tag": "exp0"},
        "judge": {"output_status": "SUFFICIENT"},
        "metrics": {"grounding_rate": 0.9},
        "citations": {"citation_precision": 0.8},
        "cost_and_trace": {"total_latency_ms": 1200, "total_cost_usd": 0.03},
    }
    cat_json = tmp_path / "single.summary.json"
    cat_json.write_text(json.dumps(catalyst_summary), encoding="utf-8")

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "g013_compare_report.py",
            "--direct-json",
            str(direct_json),
            "--catalyst-input",
            str(cat_json),
            "--out-dir",
            str(out_dir),
        ],
    )

    rc = mod.main()
    assert rc == 0
    assert (out_dir / "g013_dual_track_compare.json").exists()
    assert (out_dir / "g013_dual_track_compare.md").exists()


def test_reliability_verdict_insufficient_direct_vs_strong_catalyst():
    mod = load_script_module(str(SCRIPT_PATH), "g013_compare_report_verdict_a")
    direct = {"status_class": "INSUFFICIENT", "hallucination_flag": False}
    catalyst = {"output_status": "SUFFICIENT", "grounding_rate": 0.9, "citation_precision": 0.85}
    assert mod._reliability_verdict(direct, catalyst) is True


def test_reliability_verdict_sufficient_direct_without_hallucination():
    mod = load_script_module(str(SCRIPT_PATH), "g013_compare_report_verdict_b")
    direct = {"status_class": "SUFFICIENT", "hallucination_flag": False}
    catalyst = {"output_status": "SUFFICIENT", "grounding_rate": 0.9, "citation_precision": 0.85}
    assert mod._reliability_verdict(direct, catalyst) is False


def test_reliability_verdict_system_error_direct():
    mod = load_script_module(str(SCRIPT_PATH), "g013_compare_report_verdict_c")
    direct = {"status_class": "SYSTEM_ERROR", "hallucination_flag": False}
    catalyst = {"output_status": "SUFFICIENT", "grounding_rate": 0.9, "citation_precision": 0.85}
    assert mod._reliability_verdict(direct, catalyst) is True
