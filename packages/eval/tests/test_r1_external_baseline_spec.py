from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_r1_spec_has_protocol_requirements():
    p = PROJECT_ROOT / "docs/plans/2026-05-15-r1-external-baseline-spec.md"
    text = p.read_text(encoding="utf-8")
    assert "20-case sample" in text
    assert "same evidence chunks" in text
    assert "Pearson" in text
    assert "No cloud rerun required" in text
