from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_r0_addendum_sections_exist():
    p = PROJECT_ROOT / "docs/reports/2026-05-15-r0-baseline-addendum.md"
    text = p.read_text(encoding="utf-8")
    assert "Trivial Baseline" in text
    assert "Dual-Track Metrics" in text
    assert "Raw vs Coverage-Adjusted" in text
