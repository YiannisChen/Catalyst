from __future__ import annotations

from pathlib import Path


def test_batch_script_has_no_placeholder_echo_commands():
    text = (Path(__file__).resolve().parents[3] / "scripts" / "reports" / "run_scheme_c_batch.sh").read_text(encoding="utf-8")
    assert "run catalyst sweep" not in text
    assert "command here" not in text


def test_batch_script_generates_stability_before_bundle():
    text = (Path(__file__).resolve().parents[3] / "scripts" / "reports" / "run_scheme_c_batch.sh").read_text(encoding="utf-8")
    assert "run_direct_llm_stability_replay.py" in text
    assert "build_industrial_audit_bundle.py" in text
