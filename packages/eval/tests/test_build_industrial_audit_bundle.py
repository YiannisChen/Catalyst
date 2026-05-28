from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "reports" / "build_industrial_audit_bundle.py"
    spec = importlib.util.spec_from_file_location("build_industrial_audit_bundle", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_bundle_contains_required_artifacts(tmp_path: Path):
    mod = _load_module()
    (tmp_path / "metrics.json").write_text(
        '{"catalyst":{"system_error_count":0},"direct_llm":{"system_error_count":0}}',
        encoding="utf-8",
    )
    (tmp_path / "stability.json").write_text('{"status_consistency_rate":1.0}', encoding="utf-8")
    for name in ("per_case.jsonl", "adjudication.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")

    bundle = mod.build_bundle(
        freeze_id="f1",
        metrics_json=tmp_path / "metrics.json",
        per_case_jsonl=tmp_path / "per_case.jsonl",
        stability_json=tmp_path / "stability.json",
        adjudication_md=tmp_path / "adjudication.md",
    )
    assert bundle["artifacts"]["metrics_json"]
    assert bundle["artifacts"]["per_case_jsonl"]
    assert bundle["artifacts"]["stability_json"]
    assert bundle["artifacts"]["adjudication_md"]
    assert bundle["quality_gates"]["system_error_zero"] is True
