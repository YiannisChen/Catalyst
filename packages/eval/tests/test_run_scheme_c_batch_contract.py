from __future__ import annotations

from pathlib import Path


def test_batch_script_has_no_placeholder_echo_commands():
    text = (Path(__file__).resolve().parents[3] / "scripts" / "reports" / "run_scheme_c_batch.sh").read_text(encoding="utf-8")
    assert "run catalyst sweep" not in text
    assert "command here" not in text
    assert "cp data/eval_reports/final_rerun_20260517_131820_g2_risky_rerun_p1_ablation.json" not in text


def test_batch_script_generates_stability_before_bundle():
    text = (Path(__file__).resolve().parents[3] / "scripts" / "reports" / "run_scheme_c_batch.sh").read_text(encoding="utf-8")
    assert "run_direct_llm_stability_replay.py" in text
    assert "build_industrial_audit_bundle.py" in text
    assert "CATALYST_M_THRESHOLD=0.40" in text and "p1_ablation.py" in text
    assert "CATALYST_M_THRESHOLD=0.45" in text
    assert "CATALYST_M_THRESHOLD=0.60" in text
    assert "--allow-legacy-fallback" not in text
